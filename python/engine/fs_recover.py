"""
Recovering a floor plan from line work that names nothing.
==========================================================

`fs_clean` builds a plan from what a drawing *declares*: a layer that says
wall, a block that says chair, a polygon on a space overlay. This module is
for the drawings that declare none of it.

A DXF plotted from a PDF is the case it exists for. Every stroke lands on one
layer called something like `PDF3_Geometry`, there are no blocks, `$INSUNITS`
is unset or wrong, and the line work of a wall is identical to the line work
of a desk. A person reads the plan perfectly. Nothing in it can be read by a
rule.

The first attempt at these files looked for a per-stroke signal and found
none, which is the right answer to the wrong question. Layer, colour,
lineweight and segment length are all attributes of one stroke, and a flatten
makes every one of them uniform. What survives a flatten is **relational**:

  * what repeats, dozens of times, congruently
  * what runs collinear for forty metres, broken at every door
  * what has a parallel partner a wall's thickness away
  * what encloses a region the size and shape of a room
  * what pairs an arc with a straight leaf of its own radius

Each of those is a fact about a group of strokes rather than one stroke, and
each of them is still there after everything else has been thrown away.

The stages, in the order they have to run:

  1. `find_annotation`   strip traced text, before it pollutes every count
  2. `measure_anchors`   measure the drawing against known real-world sizes
  3. `detect_scale`      settle a continuous scale, not one of six units
  4. `classify_recovered`  walls, doors and provisional furniture
  5. `refine_recovered`  name the furniture from shapes that repeat
  6. `classify_by_contents`  name a room from what stands in it

Nothing here reads a name, so nothing here is certain. Everything this module
classifies is marked `geometry`, and the model carries a warning saying the
whole plan is inferred. That is the honest report and it is not a formality:
a recovered plan is a good starting point and a bad source of truth.
"""

from __future__ import annotations

import bisect
import math
from collections import Counter, defaultdict

import fs_clean
import fs_taxonomy as tax
import fs_units

# ─────────────────────────────────────────────────────────────────────────────
# Reading shapes back out of flattened geometry
# ─────────────────────────────────────────────────────────────────────────────

# A line drawn a fraction off true still runs along a wall. Real drawings are
# not exactly axis-aligned, and a test of exact equality throws away most of
# a converted drawing's wall faces.
AXIS_TOL_M = 0.002


def axis_segments(paths, tol=AXIS_TOL_M):
    """Axis-aligned pieces of a path list, as (axis, position, low, high).

    Axis 0 runs horizontally at a constant y, axis 1 vertically at a constant
    x. Slanted pieces are skipped: a wall thickness is measured across a
    wall, and a diagonal pair needs a projection this does not attempt.

    Unlike `fs_units._axis_segments` this allows a tolerance, because these
    coordinates have been through a unit conversion and a curve flattening
    and no longer compare exactly equal.
    """
    out = []
    for path, _ in paths:
        for index in range(len(path) - 1):
            (x0, y0), (x1, y1) = path[index], path[index + 1]
            dx, dy = abs(x1 - x0), abs(y1 - y0)
            if dy <= tol and dx > tol:
                out.append((0, (y0 + y1) / 2.0, min(x0, x1), max(x0, x1)))
            elif dx <= tol and dy > tol:
                out.append((1, (x0 + x1) / 2.0, min(y0, y1), max(y0, y1)))
    return out


def fit_arc(points):
    """Return (cx, cy, radius, sweep_degrees) for a flattened arc, or None.

    An arc reaches this module as a polyline, because flattening is what
    turns a DXF into geometry the raster can use. The circle is recovered
    from three of its points and then checked against all of them, so a
    polyline that merely looks like an arc at three places is rejected.

    The sweep is summed step by step rather than taken as the difference
    between the first and last angle. That difference is ambiguous: it cannot
    tell a 10 degree arc from a 350 degree one going the other way, and a
    drawing carries both.
    """
    if len(points) < 3:
        return None
    ax, ay = points[0]
    bx, by = points[len(points) // 2]
    cx_, cy_ = points[-1]

    d = 2.0 * (ax * (by - cy_) + bx * (cy_ - ay) + cx_ * (ay - by))
    if abs(d) < 1e-12:
        return None                    # the three points are collinear
    a2, b2, c2 = ax * ax + ay * ay, bx * bx + by * by, cx_ * cx_ + cy_ * cy_
    cx = (a2 * (by - cy_) + b2 * (cy_ - ay) + c2 * (ay - by)) / d
    cy = (a2 * (cx_ - bx) + b2 * (ax - cx_) + c2 * (bx - ax)) / d
    radius = math.hypot(ax - cx, ay - cy)
    if radius <= 0:
        return None

    # Every point has to be on that circle. Flattening puts points on the
    # true arc to within its tolerance, so the slack is generous but finite.
    limit = max(radius * 0.05, 1e-9)
    for x, y in points:
        if abs(math.hypot(x - cx, y - cy) - radius) > limit:
            return None

    sweep = 0.0
    previous = math.atan2(ay - cy, ax - cx)
    for x, y in points[1:]:
        angle = math.atan2(y - cy, x - cx)
        step = (angle - previous) % (2.0 * math.pi)
        if step > math.pi:
            step -= 2.0 * math.pi      # the short way round, either direction
        sweep += abs(step)
        previous = angle
    return cx, cy, radius, math.degrees(sweep)


def straight_runs(paths, tol=AXIS_TOL_M):
    """Straight two-point pieces of a path list, as (x0, y0, x1, y1, length).

    Used to find a door's leaf, which is a straight run from the hinge.
    """
    out = []
    for path, _ in paths:
        for index in range(len(path) - 1):
            (x0, y0), (x1, y1) = path[index], path[index + 1]
            length = math.hypot(x1 - x0, y1 - y0)
            if length > tol:
                out.append((x0, y0, x1, y1, length))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Door swings
# ─────────────────────────────────────────────────────────────────────────────
#
# The single most useful thing left in a flattened plan. A door in plan is
# drawn as a quarter-circle swing plus the leaf that sweeps it, and the arc's
# radius *is* the door's width. Door widths are constrained by how wide a
# person is, so one measured swing fixes the drawing's scale.
#
# The arc alone is not enough, and this is where a first look at these files
# went wrong: the Nasdaq drawing holds 2,633 quarter-arcs at one radius, and
# they are the rounded corners of a chair symbol, not doors. A rounded corner
# has no leaf. A door swing has a straight run, of the same length as the
# radius, starting at the arc's own centre. Testing for the pair is what
# separates the two, and nothing else does.

# A door swing as drawn is a quarter turn. Real drawings vary it, and a 45
# degree swing is common on a double door.
DOOR_SWEEP_LO, DOOR_SWEEP_HI = 40.0, 130.0
# How close the leaf's end must sit to the arc's centre, as a fraction of the
# radius. The hinge is the same point in both, up to drawing slop.
DOOR_HINGE_FRAC = 0.12
# How close the leaf's length must be to the radius. They are the same line
# swept, so this is tight.
DOOR_LEAF_FRAC = 0.15


def match_door_swings(arcs, lines):
    """Pair each arc with a leaf, and return the ones that have one.

    `arcs` is a list of (key, cx, cy, radius, sweep). `lines` is a list of
    (x0, y0, x1, y1, length). Returns a list of (key, radius).

    Unit-agnostic on purpose. It runs twice on a drawing: once in raw units
    against the document, to work the scale out before anything is scaled,
    and once in metres against the flattened items, to mark the doors. One
    implementation, so the two can never drift apart.
    """
    swings = [a for a in arcs if DOOR_SWEEP_LO <= a[4] <= DOOR_SWEEP_HI]
    if not swings or not lines:
        return []

    # Bucket the line ends so each arc is compared with its neighbours only.
    # The cell is the median radius, which is the scale of the thing being
    # matched and so is the right size whatever unit the drawing is in.
    radii = sorted(a[3] for a in swings)
    cell = max(radii[len(radii) // 2], 1e-9)
    ends = defaultdict(list)
    for x0, y0, x1, y1, length in lines:
        for x, y in ((x0, y0), (x1, y1)):
            ends[(int(math.floor(x / cell)), int(math.floor(y / cell)))].append(
                (x, y, length))

    found = []
    for key, cx, cy, radius, _ in swings:
        hinge = radius * DOOR_HINGE_FRAC
        gx, gy = int(math.floor(cx / cell)), int(math.floor(cy / cell))
        for ox in (-1, 0, 1):
            for oy in (-1, 0, 1):
                for x, y, length in ends.get((gx + ox, gy + oy), ()):
                    if abs(length - radius) > radius * DOOR_LEAF_FRAC:
                        continue
                    if math.hypot(x - cx, y - cy) <= hinge:
                        found.append((key, radius))
                        break
                else:
                    continue
                break
            else:
                continue
            break
    return found


def doc_door_swings(doc):
    """Door-swing radii in the drawing's own raw units.

    Reads the geometry straight off the document, so it can run before a
    scale is known. A flattened drawing has no blocks, so every arc and line
    in it is a top-level entity and none is missed by not recursing.

    Returns (radii, unreadable). The second is a count, not a detail: an
    entity that will not give up its geometry is dropped, and the caller
    reports how many. A silent drop here reads as a drawing with no doors,
    and this is the anchor the whole scale rests on.

    A leaf is any straight run, not only a `LINE`. Reading `LINE` alone was
    tried and found nothing at all on a real flattened drawing, whose door
    leaves came across inside polylines: the arcs were there, the leaves were
    there, and no pair was made because half the pairing was not being
    looked at. The scale then fell to the anchors that were left and came out
    a factor of two wrong.
    """
    arcs, lines = [], []
    unreadable = 0
    for index, entity in enumerate(doc.modelspace()):
        kind = entity.dxftype()
        try:
            if kind == "ARC":
                centre = entity.dxf.center
                start = float(entity.dxf.start_angle)
                end = float(entity.dxf.end_angle)
                sweep = (end - start) % 360.0
                # A zero sweep draws nothing. Adding a turn to it makes a
                # full circle out of a degenerate arc, which is how thirteen
                # arcs with 4,500 km radii once drew a star across a plan.
                if sweep <= 0.0:
                    continue
                arcs.append((index, centre[0], centre[1],
                             float(entity.dxf.radius), sweep))
                continue
            if kind == "LINE":
                s, e = entity.dxf.start, entity.dxf.end
                lines.append((s[0], s[1], e[0], e[1],
                              math.hypot(e[0] - s[0], e[1] - s[1])))
                continue
            # Every other straight run in the drawing, so a leaf drawn as
            # one segment of a polyline is still a leaf.
            points = fs_units._polyline_points(entity)
            for position in range(len(points) - 1):
                (x0, y0), (x1, y1) = points[position], points[position + 1]
                length = math.hypot(x1 - x0, y1 - y0)
                if length > 0:
                    lines.append((x0, y0, x1, y1, length))
        except Exception:
            if kind in ("ARC", "LINE"):
                unreadable += 1
            continue
    return ([radius for _, radius in match_door_swings(arcs, lines)],
            unreadable)


def item_door_swings(items):
    """Indices of items that are a door swing, with the width each implies.

    The same test as `doc_door_swings`, in metres, against flattened items.
    """
    arcs, lines = [], []
    for index, item in enumerate(items):
        if item.bbox is None:
            continue
        for path, closed in item.paths:
            if closed:
                continue
            fitted = fit_arc(path)
            if fitted:
                arcs.append((index, fitted[0], fitted[1], fitted[2], fitted[3]))
        lines.extend(straight_runs(item.paths))
    return match_door_swings(arcs, lines)


# ─────────────────────────────────────────────────────────────────────────────
# Stair ladders
# ─────────────────────────────────────────────────────────────────────────────
#
# A stair flight in plan is a run of parallel lines at a constant spacing,
# and that spacing is the tread depth. Building codes constrain a tread far
# more tightly than they constrain anything else in a plan, so a flight is a
# better scale anchor than a wall is. The catch is that a drawing may hold no
# stair at all, so this corroborates rather than decides.

# Fewer than this many parallel lines is not a flight, it is a coincidence.
# Eight rather than six because a real flight has ten to eighteen treads,
# and a plan holds plenty of shorter regular runs that are not stairs: a
# curtain-wall mullion grid, a row of desks, a run of lockers. One of those
# gave a 537 mm "tread" on a real drawing and pulled the scale with it.
LADDER_MIN_RUN = 8
# How much the spacings within one flight may vary. A drawn flight is
# regular; anything less regular is a set of unrelated lines.
LADDER_MAX_JITTER = 1.3


def ladder_spacings(segments, extent, min_run=LADDER_MIN_RUN):
    """Tread spacings of every ladder of parallel equal lines found.

    `segments` is the (axis, position, low, high) form. Treads of one flight
    all span the same width, so grouping on that span collects a flight and
    separates it from everything else at the same orientation.
    """
    if extent <= 0:
        return []
    # Relative, so it holds whatever unit the drawing is in.
    tol = extent * 1e-3
    groups = defaultdict(list)
    for axis, position, low, high in segments:
        length = high - low
        if length <= tol:
            continue
        groups[(axis, round(low / max(tol, 1e-12)),
                round(length / max(tol, 1e-12)))].append(position)

    spacings = []
    for positions in groups.values():
        if len(positions) < min_run:
            continue
        positions.sort()
        steps = [b - a for a, b in zip(positions, positions[1:]) if b - a > tol]
        if len(steps) < min_run - 1:
            continue
        steps.sort()
        median = steps[len(steps) // 2]
        if median <= 0:
            continue
        # One flight, evenly drawn. A group whose steps vary more than this
        # is not a flight and its median means nothing.
        if steps[-1] / max(steps[0], 1e-12) <= LADDER_MAX_JITTER:
            spacings.append(median)
    return spacings


# ─────────────────────────────────────────────────────────────────────────────
# Continuous scale detection
# ─────────────────────────────────────────────────────────────────────────────
#
# `fs_units.detect_unit_scale` scores six candidate units. That is the right
# method and the wrong candidate set for a plotted drawing, whose factor is
# whatever the plot was set to: one file here is near 1 unit = 10 feet, and no
# standard unit is within a factor of three of it.
#
# So the candidate set is widened rather than the method replaced. Each anchor
# proposes the scales that would put its own measurement at a sensible
# real-world size, the six standard units are proposed as well, and every
# candidate is then scored against every anchor. A drawing in millimetres
# still has millimetres in the set and still wins on it, so this cannot do
# worse than the six-candidate vote on a file that vote already handled.

# name, hard low, hard high, soft low, soft high, weight.
#
# The weights say how tightly the real world constrains each measurement. A
# door is the most constrained thing in a building, because it is sized by
# how wide a person is, and a paired swing arc identifies one beyond doubt.
# A floor plate is the least constrained: they genuinely run from 15 m to
# 150 m across, so it can only rule out an answer that is wildly wrong.
ANCHORS = (
    ("door_width",     0.60, 1.40, 0.75, 1.05, 3.5),
    ("wall_thickness", fs_units.WALL_MIN, fs_units.WALL_MAX,
                       fs_units.WALL_IDEAL_LO, fs_units.WALL_IDEAL_HI, 3.0),
    # Weighted below the extent, low as that is, because a tread is only a
    # tread if the run really is a stair. Nothing here can confirm that, and
    # a mullion grid at 537 mm read as a tread on a real drawing and took
    # the scale with it. It corroborates; it must never decide.
    ("stair_tread",    0.20, 0.40, 0.24, 0.32, 1.0),
    ("extent",         fs_units.FLOOR_MIN, fs_units.FLOOR_MAX,
                       fs_units.FLOOR_IDEAL_LO, fs_units.FLOOR_IDEAL_HI, 1.5),
)
ANCHOR_BY_NAME = {a[0]: a for a in ANCHORS}

# Two candidate scales this close together are the same answer. Keeps the
# ranking readable and stops one anchor filling it with near-duplicates.
CANDIDATE_MERGE_FRAC = 0.005

# How much collinear ink a segment must sit on before its gap to a parallel
# neighbour counts as a wall thickness, as a fraction of the plan extent.
#
# This is what makes the thickness measurable at all on a flattened drawing.
# `fs_units` measures it on wall layers alone, and it has to: a drawing with
# its furniture exploded into modelspace carries thousands of thin desk-screen
# bands against a few hundred wall bands, and the mode of everything together
# is the screen. A flattened drawing has no wall layer to fall back on, so it
# came out at 12 mm against a real 117 mm, and every anchor downstream was
# then measured against a wall the thickness of a pencil line.
#
# A wall face is long and runs collinear with other wall faces the length of
# the building. A desk screen does not. Filtering on that before measuring
# needs no thickness of its own, so it breaks the circle. Lower than the
# wall test's own fraction, because this only needs enough faces to take a
# mode from, not every wall in the drawing.
THICKNESS_SUPPORT_FRAC = 0.05


def measure_anchors(doc):
    """Measure a drawing against the sizes a building is made of.

    Returns a dict of anchor name to raw measurement, in the drawing's own
    units. Any anchor the drawing has nothing to measure is absent rather
    than zero, so scoring can skip it instead of scoring it badly.
    """
    msp = doc.modelspace()
    boxes, per_entity = [], []
    lo_x = lo_y = float("inf")
    hi_x = hi_y = float("-inf")

    for entity in msp:
        points = fs_units._polyline_points(entity)
        if not points:
            continue
        boxes.append(fs_units._bbox(points))
        per_entity.append(fs_units._axis_segments(points))
        for x, y in points:
            lo_x, lo_y = min(lo_x, x), min(lo_y, y)
            hi_x, hi_y = max(hi_x, x), max(hi_y, y)

    out = {}
    cluster = fs_units.floor_cluster(boxes)
    if cluster:
        bx0, by0, bx1, by1 = cluster["bbox"]
        extent = max(bx1 - bx0, by1 - by0)
    elif hi_x > lo_x:
        extent = max(hi_x - lo_x, hi_y - lo_y)
    else:
        extent = 0.0
    if extent > 0:
        out["extent"] = extent

    # Traced text out, before anything else is measured. A room name drawn as
    # glyph outlines is a row of small notched boxes, and the gaps inside
    # those notches are the wrong size for a wall by exactly enough to win
    # the thickness mode. This is the same detector the flattened pass uses,
    # run in raw units against a bound relative to the plan.
    glyphs = set()
    real_text = sum(1 for e in msp if e.dxftype() in ("TEXT", "MTEXT"))
    out["_real_text"] = real_text
    if extent > 0 and real_text < TEXT_PRESENT_MIN:
        glyphs, glyph_report = find_annotation_boxes(
            boxes, GLYPH_MAX_EXTENT_FRAC * extent,
            GLYPH_MIN_EXTENT_FRAC * extent)
        out["_glyphs_ignored"] = len(glyphs)
        if glyph_report["rejected"]:
            out["_glyph_detection_rejected"] = 1
    segments = [segment
                for index, group in enumerate(per_entity)
                if index not in glyphs
                for segment in group]

    swings, unreadable = doc_door_swings(doc)
    door_width = fs_units._mode_of(swings) if swings else None
    if door_width:
        out["door_width"] = door_width
        out["_door_count"] = len(swings)
    # Counted, never swallowed. This is the anchor the scale rests on, and a
    # file whose arcs all fail to read would otherwise look exactly like a
    # file with no doors in it.
    if unreadable:
        out["_arcs_unreadable"] = unreadable

    if extent > 0:
        # Wall faces only, judged by how far they run collinear with other
        # faces. Measuring across every parallel pair in the drawing gives
        # the gap inside a desk screen, because a flattened drawing holds
        # thousands of those and a few hundred walls.
        faces = well_supported(segments, extent, THICKNESS_SUPPORT_FRAC)
        gaps = fs_units.pair_gaps(
            faces, fs_units.BAND_MAX_SHORT_FRAC * extent,
            min_overlap=fs_units.PAIR_MIN_OVERLAP_FRAC * extent)
        if len(gaps) < fs_units.WALL_PAIR_MIN_SAMPLES:
            # Not enough long faces to take a mode from. Fall back to every
            # pair in the drawing.
            gaps = fs_units.pair_gaps(
                segments, fs_units.BAND_MAX_SHORT_FRAC * extent,
                min_overlap=fs_units.PAIR_MIN_OVERLAP_FRAC * extent)
            faces = []
        # `_mode_of` returns None when nothing in the list is positive, and a
        # count test does not rule that out. Nothing downstream guards
        # against a None here, so it is refused at the source rather than
        # relying on `pair_gaps` happening to floor its gaps above zero.
        thickness = (fs_units._mode_of(gaps)
                     if len(gaps) >= fs_units.WALL_PAIR_MIN_SAMPLES else None)
        if thickness:
            out["wall_thickness"] = thickness
            out["_wall_pair_count"] = len(gaps)
            out["_wall_faces"] = len(faces)
            # The whole population, kept for a second pass. The mode of it
            # is the best guess available before a scale is known, and on a
            # flattened drawing it is often wrong: this file's gaps run
            # 11.6, 23.3, 34.8, 47.0, 57.4 -- a family of glazing and
            # desk-screen lines at multiples of one spacing -- and the real
            # 115.6 wall is only the sixth most common. Once a scale exists,
            # gaps too thin to be a wall can be excluded outright, and the
            # mode of what is left is the wall. That filter needs the scale,
            # so it cannot run here.
            out["_gaps"] = gaps

        treads = ladder_spacings(segments, extent)
        if treads:
            out["stair_tread"] = fs_units._mode_of(treads)
            out["_ladder_count"] = len(treads)

    return out


def well_supported(segments, extent, fraction):
    """Segments that lie on a line carrying a lot of collinear ink.

    Needs no thickness of its own, only a length judged against the plan, so
    it can run before either is known. That is what makes it usable as the
    filter under the thickness measurement.
    """
    if extent <= 0:
        return list(segments)
    # Wall faces are exactly collinear in a CAD drawing, so this only has to
    # absorb the slop a unit conversion leaves.
    bucket = max(extent * 1e-4, 1e-9)
    spans = defaultdict(list)
    for segment in segments:
        spans[(segment[0], round(segment[1] / bucket))].append(segment)

    needed = extent * fraction
    out = []
    for members in spans.values():
        intervals = sorted((s[2], s[3]) for s in members)
        union = 0.0
        cursor = intervals[0][0]
        for low, high in intervals:
            cursor = max(cursor, low)
            if high > cursor:
                union += high - cursor
                cursor = high
        if union >= needed:
            out.extend(members)
    return out


def _candidate_scales(measured):
    """Every scale worth scoring, from the anchors and the standard units."""
    candidates = list(fs_units.CANDIDATES.values())
    for name, hard_lo, hard_hi, soft_lo, soft_hi, _ in ANCHORS:
        raw = measured.get(name)
        if not raw or raw <= 0:
            continue
        # Three points across the plausible range, not just its centre. An
        # anchor at the edge of its range can still be the one that is right,
        # and offering only the centre makes that answer unreachable.
        for target in (soft_lo, (soft_lo + soft_hi) / 2.0, soft_hi):
            candidates.append(target / raw)

    candidates = [c for c in candidates if c > 0 and math.isfinite(c)]
    candidates.sort()
    merged = []
    for value in candidates:
        if merged and abs(math.log(value / merged[-1])) < CANDIDATE_MERGE_FRAC:
            continue
        merged.append(value)
    return merged


def score_scale(scale, measured):
    """Score one candidate against every anchor. Returns (score, centrality, detail).

    Two numbers, because the first one ties. `_fit_score` returns 1.0
    anywhere inside an anchor's soft range, and those ranges are wide: a door
    is anything from 0.75 m to 1.05 m. So a whole interval of scales scores a
    perfect 1.0 and the winner among them is decided by nothing at all.

    That is not a hypothetical. On the flattened fixture the correct scale of
    0.001 and a scale of 0.000833 both scored 1.0 on all three anchors, and
    the wrong one won on list order, putting a 24 m floor at 20 m. Every
    individual measurement still landed in range, which is the same signature
    as the scale bug this whole pipeline exists to prevent.

    `centrality` breaks it. It measures how near the middle of its range each
    anchor lands, so a door at 0.90 m beats the same door read as 0.75 m even
    though both are allowed. It only ever ranks candidates that the real
    score cannot separate, so it never overrides a genuine fit.
    """
    total = weight_used = 0.0
    centre_total = 0.0
    detail = {}
    for name, hard_lo, hard_hi, soft_lo, soft_hi, weight in ANCHORS:
        raw = measured.get(name)
        if not raw or raw <= 0:
            continue
        metres = raw * scale
        sub = fs_units._fit_score(metres, hard_lo, hard_hi, soft_lo, soft_hi)
        # Distance from the middle of the soft range, in log terms, as a
        # fraction of the range's own width. Geometric rather than linear,
        # because a range from 0.75 to 1.05 is a ratio and not an interval.
        ideal = math.sqrt(soft_lo * soft_hi)
        span = math.log(soft_hi / soft_lo)
        near = 1.0 - abs(math.log(max(metres, 1e-12) / ideal)) / max(span, 1e-9)
        detail[name] = {"metres": metres, "score": sub,
                        "centrality": max(0.0, min(1.0, near))}
        total += sub * weight
        centre_total += max(0.0, min(1.0, near)) * weight
        weight_used += weight
    if weight_used == 0:
        return 0.0, 0.0, detail
    return total / weight_used, centre_total / weight_used, detail


def detect_scale(doc):
    """Return (metres_per_unit, report) for a drawing that declares nothing.

    Same shape of report as `fs_units.detect_unit_scale`, so the rest of the
    pipeline needs no knowledge of which one produced it.
    """
    measured = measure_anchors(doc)
    ranked = _rank_scales(measured)
    if not ranked or ranked[0]["score"] <= 0.0:
        # Nothing measurable. Metres is the least destructive assumption:
        # too small a scale asks for a raster of hundreds of millions of
        # cells and the build stops being one that finishes.
        return 1.0, {
            "unit": "unknown", "nearest_unit": "m", "scale": 1.0,
            "source": "no evidence", "confidence": 0.0, "margin": None,
            "decided_by": "nothing", "centrality": 0.0,
            "wall_thickness_m": None, "wall_thickness_source": "not measured",
            "header_insunits": None,
            "header_unit": None, "header_agrees": None,
            "anchors": {}, "anchor_counts": {}, "evidence_raw": {},
            "implied_metres": {}, "ranking": [], "floor_cluster": None,
        }

    # One round of refinement, and only one.
    #
    # The thickness measured before a scale is known is the mode of every
    # parallel pair in the drawing, and on a flattened drawing that is
    # usually a glazing line rather than a wall. With a scale in hand, gaps
    # too thin to be a wall can be excluded and the mode of what is left is
    # the wall. That is a better measurement, so the scale is chosen again
    # against it.
    #
    # It stops there deliberately. Each pass feeds the other, and iterating
    # to convergence would make the answer depend on where it started, which
    # is exactly the kind of fault that reads as plausible and is not.
    refined, source = _refine_thickness(measured, ranked[0]["scale"])
    if refined and refined != measured.get("wall_thickness"):
        measured = dict(measured, wall_thickness=refined)
        ranked = _rank_scales(measured)
        refined, source = _refine_thickness(measured, ranked[0]["scale"])
        # The refined value goes back into `measured` as well, or the report
        # describes two different measurements: `wall_thickness_m` from the
        # second pass, and `anchors.wall_thickness` from the first, with the
        # confidence scoring a fit to the older one.
        measured = dict(measured, wall_thickness=refined)

    return _scale_report(ranked, measured, refined, source)


def _refine_thickness(measured, scale):
    """The wall thickness again, with gaps too thin to be a wall excluded."""
    plausible = [gap for gap in measured.get("_gaps") or ()
                 if fs_units.WALL_MIN <= gap * scale <= fs_units.WALL_MAX]
    if len(plausible) >= fs_units.WALL_PAIR_MIN_SAMPLES:
        return fs_units._mode_of(plausible), "mode of the pairs a wall could be"
    return measured.get("wall_thickness"), "mode of every pair"


def _rank_scales(measured):
    """Every candidate scale, scored and ordered best first."""
    ranked = []
    for scale in _candidate_scales(measured):
        score, centrality, detail = score_scale(scale, measured)
        # A standard unit, preferred on a tie. This is a prior, and a strong
        # one: drawings are authored in real units far more often than at
        # arbitrary plot factors. It only ever breaks a tie, so a drawing
        # genuinely plotted at 1 unit = 10 feet still wins on fit, there
        # being no standard unit within a factor of three of it.
        #
        # Without it, centrality decides, and centrality is biased whenever
        # an anchor's true value is not at the middle of its range. This
        # file's doors are 929 mm, so centrality preferred the scale putting
        # them at 887 mm -- the middle of the door range -- and landed 3%
        # short of millimetres with every anchor still scoring 1.0.
        standard = fs_units.nearest_unit(scale)[2] <= 1e-9
        ranked.append({"scale": scale, "score": score, "standard": standard,
                       "centrality": centrality, "detail": detail})
    # Fit first, then a standard unit, then how centrally it fits. Never any
    # other order: a candidate that puts one anchor dead centre and another
    # out of range is worse than one that puts both comfortably inside.
    ranked.sort(key=lambda r: (-r["score"], not r["standard"],
                               -r["centrality"], r["scale"]))
    return ranked


def _scale_report(ranked, measured, thickness, thickness_source):
    """Turn a ranking into the report the rest of the pipeline reads."""
    best = ranked[0]
    scale = best["scale"]
    # A drawing whose factor turns out to be a standard unit should say so
    # rather than report seven digits that happen to equal one.
    name, unit_scale, error = fs_units.nearest_unit(scale)
    if error <= fs_units.UNIT_SNAP_FRAC:
        scale, unit = unit_scale, name
    else:
        unit = "custom"

    runner_up = None
    for row in ranked[1:]:
        # The next genuinely different answer, not the next candidate. Two
        # candidates a percent apart are one answer, and reporting the margin
        # between them would say every detection was a coin toss.
        if abs(math.log(row["scale"] / best["scale"])) > 0.05:
            runner_up = row
            break

    return scale, {
        "unit": unit,
        "nearest_unit": name,
        "scale": scale,
        "source": "recovered",
        "confidence": round(best["score"], 3),
        "margin": (round(best["score"] - runner_up["score"], 3)
                   if runner_up else None),
        # Which of the two comparisons actually settled it. A margin of zero
        # with `centrality` here means several scales fitted every anchor and
        # the middle of the ranges chose between them, which is a weaker
        # answer than a margin on the fit and should read as one.
        "decided_by": ("fit" if runner_up
                       and best["score"] - runner_up["score"] > 1e-9
                       else "centrality" if runner_up
                       else "the only answer"),
        "centrality": round(best["centrality"], 3),
        "wall_thickness_m": (round(thickness * scale, 4) if thickness else None),
        "wall_thickness_source": thickness_source,
        "header_insunits": None,
        "header_unit": None,
        "header_agrees": None,
        # What each anchor measured, and what it comes to at this scale. This
        # is the whole argument for the answer, so it travels with it.
        "anchors": {
            anchor: {
                "raw": round(float(raw), 6),
                "metres": round(float(raw) * scale, 4),
                "score": round(best["detail"].get(anchor, {}).get("score", 0.0), 3),
            }
            for anchor, raw in measured.items()
            if not anchor.startswith("_") and anchor in ANCHOR_BY_NAME
        },
        # Counts only. `_gaps` holds the whole gap population, thousands of
        # floats on a real drawing, and this report crosses into JavaScript
        # as JSON. It is working data, not something to publish.
        "anchor_counts": {key[1:]: value
                          for key, value in measured.items()
                          if key.startswith("_") and isinstance(value, int)},
        "evidence_raw": {key: round(float(value), 4)
                         for key, value in measured.items()
                         if not key.startswith("_")},
        "implied_metres": {name: round(float(row["metres"]), 4)
                           for name, row in best["detail"].items()},
        "ranking": [{"scale": round(row["scale"], 8),
                     "score": round(row["score"], 3)}
                    for row in ranked[:6]],
        "floor_cluster": None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Stage 1: traced text
# ─────────────────────────────────────────────────────────────────────────────
#
# This has to run first, because text pollutes every measurement after it.
# One real file holds 683 room names and desk numbers drawn as traced glyph
# outlines, with no TEXT entity anywhere in it; another draws the same labels
# as 11,808 filled hatches. Left in, those become hundreds of tiny "furniture"
# objects, they drag the wall-thickness mode down to a letter stroke, and they
# repeat congruently often enough to look like a chair.
#
# A letter is small, it sits in a horizontal row with others of its own
# height, and it very nearly touches its neighbours. Nothing structural does
# all three. The gap is what does the work: letters in a word are a fraction
# of a cap height apart, where a row of chairs at a bench is spaced by its
# own width.

# A glyph is no taller than this, in metres. Plan text is 0.2 m to 0.4 m at
# the scales these drawings use.
GLYPH_MAX_M = 0.5
# And no shorter than this. Bounding only the top was wrong, and on a real
# drawing it was badly wrong: text has a minimum size too, because a person
# has to be able to read it.
#
# Without a floor, "small" includes every sub-centimetre fragment in the
# file, and a drawing whose furniture is exploded holds them in their
# thousands. On the flattened real file this detector claimed 1,604 glyphs in
# 397 words, all of them 1 mm to 13 mm across: 700 splines, 620 polylines and
# 284 arcs of furniture detail, sitting in rows, closely spaced and all much
# the same size. They passed every test the detector had. The file's actual
# labels are 479 real MTEXT entities and none of them was involved.
#
# 50 mm is below any text a plan carries and far above the detail fragments.
GLYPH_MIN_M = 0.05
# The same two bounds for the pass that runs before a scale is known, as
# fractions of the plan's extent. Text on a floor plan is a small share of
# the plan whatever unit the plan is drawn in, so a relative bound is the
# only one available there -- and it is the same reasoning `fs_units` uses
# for its own pre-scale band limits.
#
# The floor is 0.001, which is 24 mm on a 24 m fixture and 79 mm on a 78 m
# floor. That keeps a 100 mm desk number on a large plan and still excludes
# the millimetre-scale detail above.
GLYPH_MAX_EXTENT_FRAC = 0.025
GLYPH_MIN_EXTENT_FRAC = 0.001
# The largest gap between two letters, as a fraction of their height. This is
# the discriminator: below half a cap height is a word, above it is a row of
# objects that happen to be in line.
GLYPH_MAX_GAP_FRAC = 0.5
# Fewer letters than this in a row is not a word.
GLYPH_MIN_RUN = 4
# Two glyphs are the same size within this fraction. Letters in one label
# share a cap height; unrelated small objects do not.
GLYPH_HEIGHT_TOL = 0.4
# If glyph detection claims more than this share of the drawing, it is wrong
# about something and its answer is thrown away. A plan is not mostly text,
# and a detector that thinks so would delete the building.
GLYPH_MAX_SHARE = 0.35

# Real text entities before the drawing is taken to have kept its labels, in
# which case glyph detection does not run at all.
#
# This is the gate that matters, and it took a real drawing to see why. The
# size, row, spacing and height tests together are not enough to identify
# text on a furniture-dense plan: rows of small, similar, closely spaced
# objects are everywhere in one. Chair arms, desk fittings and cable trays
# all qualify. On the flattened real file the detector claimed 1,203 glyphs
# in 275 words, and the correct answer for that file is **zero** -- its
# labels came across as 479 real MTEXT entities and it holds no traced text
# at all. Every one of those 1,203 was a false positive, and each one took a
# piece of furniture out of the plan with it.
#
# So the question to ask first is not "does this look like a letter" but
# "did this drawing lose its labels at all". Glyph detection exists for one
# case: a file with no TEXT and no MTEXT anywhere, whose room names are
# outlines. A drawing that still has its labels has nothing for it to find
# and everything for it to break.
#
# Eight, so that a title block or a couple of stray notes does not count as
# a label set and switch the detection off on a drawing that does need it.
TEXT_PRESENT_MIN = 8


def find_annotation_boxes(boxes, max_size, min_size=0.0, total=None):
    """Indices of boxes that sit in a row like the letters of a word.

    `boxes` is a list of (x0, y0, x1, y1). Returns (indices, report).

    Written against plain boxes rather than items so that both callers can
    share it. One runs after flattening, in metres, to keep traced text out
    of the plan. The other runs before flattening, in raw units, to keep the
    same text out of the measurements the scale is worked out from. That
    second one matters more than it looks: the glyph fixture's letters are
    150 by 250 mm boxes with a notch, and left in the wall-thickness mode
    they moved a 120 mm wall to 100 mm.
    """
    candidates = []
    for index, (x0, y0, x1, y1) in enumerate(boxes):
        width, height = x1 - x0, y1 - y0
        if width <= 0 or height <= 0:
            continue
        if max(width, height) > max_size:
            continue
        # Big enough to be a letter as well as small enough. The height is
        # what a cap height is, so it is the side that has to clear the
        # floor; a narrow letter like an I is still tall.
        if height < min_size:
            continue
        candidates.append(index)

    # Rows first. A label is a horizontal run, so group by which band of the
    # drawing each glyph's centre line falls in before looking along it.
    #
    # Two things about the key, and both are load-bearing.
    #
    # **One divisor for every glyph**, taken as the median candidate height.
    # Dividing by each glyph's own height instead makes the divisor vary
    # across the row, so two letters at the same height on the page land in
    # different bands whenever their drawn heights differ at all.
    #
    # **Measured from the lowest candidate**, not from the drawing origin.
    # `measure_anchors` runs this on raw DXF coordinates, before any scale is
    # known, and a drawing on survey coordinates puts its text at y = 250000.
    # An absolute key there is `250000 / 250` = 1000, so a 0.1% height
    # difference between two letters moves the key by a whole band and the
    # row never forms. The same drawing translated to the origin works. That
    # made text detection depend on where the building sits, which nothing
    # about reading a label should, and it silently disabled the height
    # tolerance below: no two letters could reach the same band to be
    # compared.
    rows = defaultdict(list)
    if candidates:
        heights = sorted(boxes[i][3] - boxes[i][1] for i in candidates)
        band = max(heights[len(heights) // 2], 1e-9)
        origin = min(boxes[i][1] for i in candidates)
        for index in candidates:
            _, y0, _, y1 = boxes[index]
            key = ((y0 + y1) / 2.0 - origin) / band
            # Two offset buckets, so a row sitting across a band boundary is
            # not cut in half.
            for offset in (0, 1):
                rows[(round(key - 0.5 * offset), offset)].append(index)

    found = set()
    # What a run is, precisely: one word, not one label. A space advances the
    # pen without drawing, so the gap across it is wider than a letter gap
    # and the run ends there. That is the right behaviour and worth stating,
    # because the count reads oddly otherwise: six room names holding eleven
    # words come back as eleven runs.
    #
    # Held as sets so the same physical run, found in both offset buckets,
    # is counted once. Reported straight it doubled every count.
    runs = set()
    for members in rows.values():
        if len(members) < GLYPH_MIN_RUN:
            continue
        members.sort(key=lambda i: boxes[i][0])
        run = [members[0]]
        for index in members[1:]:
            pa = boxes[run[-1]]
            pb = boxes[index]
            gap = pb[0] - pa[2]
            ha, hb = pa[3] - pa[1], pb[3] - pb[1]
            height = max(ha, hb)
            similar = min(ha, hb) >= height * (1.0 - GLYPH_HEIGHT_TOL)
            # A letter nearly touches the next one. The gap is the whole
            # discriminator: below half a cap height is a word, above it is
            # a row of objects that happen to line up. A bench of chairs is
            # spaced by its own width and never passes this.
            if similar and -height <= gap <= height * GLYPH_MAX_GAP_FRAC:
                run.append(index)
                continue
            if len(run) >= GLYPH_MIN_RUN:
                found.update(run)
                runs.add(frozenset(run))
            run = [index]
        if len(run) >= GLYPH_MIN_RUN:
            found.update(run)
            runs.add(frozenset(run))

    population = total if total is not None else len(boxes)
    report = {"words": len(runs), "glyphs": len(found), "rejected": False}
    if population and len(found) > population * GLYPH_MAX_SHARE:
        # Refusing to act beats deleting the plan. Said out loud, because a
        # drawing that trips this is one worth looking at by hand.
        report["rejected"] = True
        report["glyphs"] = 0
        report["words"] = 0
        return set(), report
    return found, report


def find_annotation(items, texts=0):
    """Indices of items that are traced text rather than plan geometry.

    `texts` is how many real TEXT and MTEXT entities the drawing holds. At
    `TEXT_PRESENT_MIN` or more the drawing kept its labels, so there is no
    traced text to find and this does not run. See that constant for what
    happens when it does run anyway.
    """
    if texts >= TEXT_PRESENT_MIN:
        return set(), {"words": 0, "glyphs": 0, "rejected": False,
                       "skipped": "the drawing kept its own text labels"}
    boxes, keys = [], []
    for index, item in enumerate(items):
        if item.bbox is None:
            continue
        boxes.append(item.bbox)
        keys.append(index)
    found, report = find_annotation_boxes(boxes, GLYPH_MAX_M, GLYPH_MIN_M,
                                          total=len(items))
    return {keys[position] for position in found}, report


# ─────────────────────────────────────────────────────────────────────────────
# Stage 2: walls, from topology rather than from names
# ─────────────────────────────────────────────────────────────────────────────
#
# Two signals, and an item is a wall if either fires.
#
# A wall has a parallel partner. Its two faces run alongside each other, a
# wall's thickness apart, for as long as the wall does. A desk's two long
# edges are also parallel, but they are 700 mm apart rather than 120 mm, and
# they close into a rectangle instead of meeting other pairs at junctions.
#
# A wall runs collinear a long way. A wall crosses a building, broken at every
# door, so the segments lying on its centre line add up to metres of ink. A
# desk edge is collinear with nothing further away than its own desk.
#
# Neither alone is enough. The pair test misses a wall drawn as one centre
# line, which `thicken_thin` exists to handle downstream. The collinear test
# alone promotes any long line, including the edge of a bench run. Together
# they cover what a plan actually contains.

# A parallel partner counts as a wall face when the gap is within this band
# of the measured thickness. Wide enough for a drawing that mixes a 100 mm
# partition with a 150 mm one, narrow enough to exclude a desk's own depth.
PAIR_GAP_LO, PAIR_GAP_HI = 0.55, 1.9
# And when the two run alongside each other for at least this much, in
# metres. An absolute floor, because a test of the form "long enough relative
# to the gap" asks a sliver for almost nothing: at four times the gap, a
# 12 mm end cap qualifies on 48 mm of overlap, and a drawing holds hundreds
# of end caps. That mistake cost a whole build once.
PAIR_MIN_OVERLAP_M = 0.4
# Collinear ink needed on one line before it reads as a wall. Three tests,
# and all three are needed. Furniture laid out on a grid lines up, and each
# test on its own lets some arrangement of it through.
#
# The sum. A 1.6 m desk edge must not reach it; a partition across a room
# must. This alone marked every desk in the fixture as a wall: six desks in a
# row share the y of their front edges, so their ink adds to 9.6 m and sails
# past a 3 m floor. Every room was then cut in half.
COLLINEAR_MIN_M = 3.0
# The sum again, relative to the plan. A wall is a feature of the building
# and spans a real share of it, where furniture spans a desk. Two desks side
# by side give 3.2 m of ink and clear the absolute floor above, so on a
# 24 m plan this is what stops them.
COLLINEAR_MIN_FRAC = 0.15
# Contiguity. A wall covers its own line almost entirely, broken only where
# a door interrupts it: a partition with a doorway covers 89% of its span.
# A row of furniture dots the line and leaves most of it empty, so three
# desks spread over 8 m cover 60% and are refused here.
COLLINEAR_MIN_COVERAGE = 0.75
# Forward neighbours each segment is compared with, which bounds the cost on
# a drawing that stacks hundreds of segments at one coordinate.
PAIR_SCAN_LIMIT = 40


def infer_walls(items, wall_thickness, extent, skip=()):
    """Indices of the items that are structural, and why. Returns (set, report).

    `skip` is the annotation, which must be excluded before anything is
    measured rather than filtered out afterwards. `extent` is the plan's own
    size, which the collinear test is judged against.
    """
    segments = []          # (axis, position, low, high, item index)
    for index, item in enumerate(items):
        if index in skip or item.bbox is None:
            continue
        for axis, position, low, high in axis_segments(item.paths):
            segments.append((axis, position, low, high, index))

    by_pair = set()
    lo = wall_thickness * PAIR_GAP_LO
    hi = wall_thickness * PAIR_GAP_HI
    for axis in (0, 1):
        same = sorted((s for s in segments if s[0] == axis),
                      key=lambda s: s[1])
        positions = [s[1] for s in same]
        for position_index, (_, position, low, high, index) in enumerate(same):
            # Start at the first segment far enough away to be the other face
            # of a wall, rather than scanning forward from here and spending
            # the budget on segments that are too close to be one.
            #
            # This matters on the drawings the module is for. A flattened
            # curtain wall or a run of desk screens puts dozens of parallel
            # lines within a few millimetres, all of them nearer than `lo`.
            # Walking forward from the current segment, all 40 places in the
            # window went to those, the loop never reached the wall's second
            # face, and neither face was found. `by_collinear` covered for it,
            # so the only sign was a low `by_pair` count in the report.
            first = bisect.bisect_left(positions, position + lo,
                                       position_index + 1)
            for other in same[first:first + PAIR_SCAN_LIMIT]:
                gap = other[1] - position
                if gap > hi:
                    break
                overlap = min(high, other[3]) - max(low, other[2])
                if overlap >= PAIR_MIN_OVERLAP_M:
                    by_pair.add(index)
                    by_pair.add(other[4])

    # Collinear support. Bucket by the line each segment lies on, at half a
    # wall so the two faces of one wall do not land in the same bucket.
    bucket = max(wall_thickness * 0.5, 1e-6)
    spans = defaultdict(list)
    members = defaultdict(set)
    for axis, position, low, high, index in segments:
        key = (axis, round(position / bucket))
        spans[key].append((low, high))
        members[key].add(index)
    floor_length = max(COLLINEAR_MIN_M, extent * COLLINEAR_MIN_FRAC)
    by_collinear = set()
    for key, intervals in spans.items():
        # The union, not the sum. Two faces drawn over each other, or a
        # segment drawn twice by a conversion, would otherwise count double
        # and a duplicated desk edge would read as a wall.
        intervals.sort()
        union = 0.0
        cursor = intervals[0][0]
        for low, high in intervals:
            cursor = max(cursor, low)
            if high > cursor:
                union += high - cursor
                cursor = high
        # The greatest `high`, not the last one. `intervals` is sorted by
        # `(low, high)`, so an interval nested inside an earlier one leaves
        # the largest `high` behind it. Reading the last entry understates
        # the span, which inflates the coverage ratio and makes the test
        # below more permissive than it is meant to be.
        span = max(high for _, high in intervals) - intervals[0][0]
        if union < floor_length:
            continue
        if span > 0 and union / span < COLLINEAR_MIN_COVERAGE:
            continue
        by_collinear |= members[key]

    walls = by_pair | by_collinear
    return walls, {
        "walls": len(walls),
        "by_pair": len(by_pair),
        "by_collinear": len(by_collinear),
        "by_both": len(by_pair & by_collinear),
        "segments": len(segments),
        "collinear_min_m": round(floor_length, 3),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Stage 3: recovering the blocks the export destroyed
# ─────────────────────────────────────────────────────────────────────────────
#
# The inverse of the fault. The export exploded every block into loose
# fragments, and this puts them back: fragments that touch become an object,
# and objects with the same shape become a class.
#
# A building does not repeat its walls as congruent islands. Furniture does
# nothing else: a floor holds forty of one chair and twenty of one desk. So a
# shape with four or more congruent copies is furniture, with no name needed
# anywhere in the file. That is the strongest signal a flattened drawing has,
# and it also names what it finds, because the size of the most repeated small
# shape is a chair and the size of the next is a desk.
#
# The signature has to be invariant under rotation, because the same chair is
# drawn at every angle around a table. Hull area, hull perimeter and hull
# diameter are all invariant, and together they separate a chair from a desk
# from a plant pot. A bounding box is not invariant and must not be used: a
# desk turned 45 degrees has a box twice the area of the same desk square on.

# Congruent within this fraction, on each of the three measures.
SIGNATURE_TOL = 0.06
# Copies before a shape is a repeated symbol rather than a one-off.
REPEAT_MIN_INSTANCES = 4
# A repeated symbol is no bigger than this across, in metres. Above it the
# repetition is structural: a row of identical columns, or one bay of a
# curtain wall drawn forty times.
REPEAT_MAX_M = 3.5

# Smaller than the diagonal of the smallest whole object any size rule
# recognises -- a 0.38 x 0.38 m chair, whose own hull diameter is 0.54 m.
# Derived from `fs_taxonomy._SIZE_RULES` rather than hand-picked, so
# tightening or loosening a size rule there cannot leave this constant
# silently out of step with the rule it exists to stay under. A repeated
# shape at or above this could be a genuinely small whole object; a shape
# under it cannot be a whole piece of furniture at all -- it can only be a
# fragment of one, such as an arm rest or a chair leg that never touched the
# rest of its own chair closely enough for the first grouping pass to join
# them.
FRAGMENT_MAX_DIAMETER_M = min(
    math.hypot(long_lo, short_lo) for long_lo, _, short_lo, _, _ in tax._SIZE_RULES)

# Loose enough to bridge a fragment to the rest of its own object -- an arm
# to a seat, a leg to a top, wherever drafting left a gap the first, tight
# pass would not cross -- but still tight enough that a chair stands apart
# from the desk beside it, which is rarely closer than this.
FRAGMENT_REGROUP_GAP_M = 0.30


def _hull_signature(item):
    """(diameter, area, perimeter) of an item's convex hull, or None.

    All three are invariant under rotation and translation, which is what
    lets the same chair match itself at every angle around a table.
    """
    hull = fs_clean.convex_hull(item.all_points())
    if not hull or len(hull) < 4:
        return None
    area = abs(fs_clean.ring_area(hull))
    perimeter = sum(math.hypot(b[0] - a[0], b[1] - a[1])
                    for a, b in zip(hull, hull[1:]))
    diameter = 0.0
    for position, a in enumerate(hull):
        for b in hull[position + 1:]:
            diameter = max(diameter, math.hypot(b[0] - a[0], b[1] - a[1]))
    if diameter <= 0 or perimeter <= 0 or area <= 0:
        return None
    # A zero area has to be refused rather than bucketed. `convex_hull` of
    # three or more collinear points returns a degenerate ring of no area, so
    # every straight leftover fragment in the drawing would share one bucket:
    # `log(1e-9)` is the same number for all of them, and a straight run's
    # perimeter is just twice its diameter, so the three-measure signature
    # collapses to one. Four such fragments then look like a repeated symbol,
    # and a flattened drawing has thousands of them.
    return diameter, area, perimeter


def _signature_key(signature):
    """Bucket a signature so congruent shapes collide and others do not."""
    diameter, area, perimeter = signature
    def bucket(value):
        return round(math.log(max(value, 1e-9)) / SIGNATURE_TOL)
    return bucket(diameter), bucket(area), bucket(perimeter)


def find_repeats(items, indices=None):
    """Group items by shape. Returns (classes, report).

    `classes` maps a signature key to the list of item indices that share it,
    for the keys with enough members to be a repeated symbol.
    """
    signatures = {}
    for index in (indices if indices is not None else range(len(items))):
        item = items[index]
        if item.bbox is None:
            continue
        if max(item.width, item.height) > REPEAT_MAX_M:
            continue
        signature = _hull_signature(item)
        if signature:
            signatures[index] = signature

    grouped = defaultdict(list)
    for index, signature in signatures.items():
        grouped[_signature_key(signature)].append(index)

    classes = {key: members for key, members in grouped.items()
               if len(members) >= REPEAT_MIN_INSTANCES}
    return classes, {
        "measured": len(signatures),
        "classes": len(classes),
        "instances": sum(len(m) for m in classes.values()),
        "largest_class": max((len(m) for m in classes.values()), default=0),
    }


def merge_fragment_classes(items, classes):
    """Retry grouping on repeat classes too small to be a whole object.

    The first grouping pass keys on mere touching within one layer, and a
    flattened drawing has one layer for the whole building. It under-merges
    exactly where drafting left a gap between two pieces of the same
    object -- an arm rest that never quite touched its own chair's seat --
    and `find_repeats` then finds that fragment repeated as congruently as
    a real small object would be. The largest such class measured on a real
    flattened building had 152 members, and the building holds nowhere near
    152 of anything that shape could honestly be.

    A class this small cannot be a whole object -- see
    `FRAGMENT_MAX_DIAMETER_M` -- so its members are regrouped at a looser
    gap, the same merge the first pass uses, and left for the caller to
    measure and name again now that they may have a real object's footprint.

    Returns (items, report). `items` is the same list, unchanged, unless
    something actually regrouped, in which case it is a new one: the
    fragment-class members replaced by whatever they merged into.
    """
    fragment_indices = set()
    for members in classes.values():
        signature = _hull_signature(items[members[0]])
        if signature and signature[0] <= FRAGMENT_MAX_DIAMETER_M:
            fragment_indices.update(members)
    if not fragment_indices:
        return items, {"regrouped": 0, "into": 0}

    fragments = [items[i] for i in sorted(fragment_indices)]
    kept = [item for index, item in enumerate(items)
            if index not in fragment_indices]
    merged, group_report = fs_clean.group_loose_geometry(
        fragments, gap=FRAGMENT_REGROUP_GAP_M,
        max_span=fs_clean.LOOSE_GROUP_MAX_SPAN)
    return kept + merged, {"regrouped": group_report["grouped"],
                           "into": group_report["into"]}


# ─────────────────────────────────────────────────────────────────────────────
# The recovery passes, as the pipeline calls them
# ─────────────────────────────────────────────────────────────────────────────

def classify_recovered(items, wall_thickness, extent=None, texts=0):
    """Classify every item from shape alone. Replaces `classify_items`.

    Runs in place and returns a report. This is the pass that decides what is
    structural, so getting it wrong is expensive in both directions: a desk
    marked as a wall cuts a room in half, and a wall marked as furniture
    merges two rooms into one.

    Where it cannot tell, it says furniture. That is the safer error. Stray
    furniture sits inside a room and is visibly wrong; a stray wall changes
    the rooms and looks plausible.
    """
    annotation, glyph_report = find_annotation(items, texts)
    doors = dict(item_door_swings(items))
    if extent is None:
        boxes = [item.bbox for item in items if item.bbox is not None]
        extent = (max(max(b[2] for b in boxes) - min(b[0] for b in boxes),
                      max(b[3] for b in boxes) - min(b[1] for b in boxes))
                  if boxes else 0.0)
    walls, wall_report = infer_walls(items, wall_thickness, extent,
                                     skip=annotation | set(doors))

    for index, item in enumerate(items):
        if index in annotation:
            item.element_type, item.cls, item.sub_class = "Annotation", None, None
            item.confidence = "geometry"
        elif index in doors:
            item.element_type, item.cls, item.sub_class = "Structure", "Door", None
            item.confidence = "geometry"
        elif index in walls:
            item.element_type, item.cls, item.sub_class = "Structure", "Wall", None
            item.confidence = "geometry"
        else:
            # Provisional. Grouping has not run yet, so most of these are
            # single fragments with no footprint worth measuring. `Other`
            # rather than a guess, because `refine_recovered` names them
            # once they have been joined into objects.
            item.element_type, item.cls, item.sub_class = "Furniture", "Other", None
            item.confidence = "unknown"

    return {
        "annotation": glyph_report,
        "doors": len(doors),
        "door_width_m": (round(fs_units._mode_of(list(doors.values())), 3)
                         if doors else None),
        "walls": wall_report,
    }


def refine_recovered(items):
    """Name the furniture, now that grouping has made objects of it.

    Runs after `group_loose_geometry`, which is the earliest point at which a
    recovered object has a footprint to measure. Three passes over it: the
    size rules first, because they carry real knowledge about what a 1.6 by
    0.8 m rectangle is; then, for whatever is left, a check for repeat
    classes too small to be a whole object, which are re-grouped at a looser
    gap and measured again; then repetition for whatever the size rules still
    could not name.

    `items` may be replaced in place (`items[:] = ...`) if the middle pass
    regroups anything, so a caller holding onto the same list sees the
    result without needing this function to return one.
    """
    def name_by_size(indices):
        named = 0
        for index in indices:
            item = items[index]
            guess = tax.classify_by_size(*item.size())
            if guess:
                item.element_type, item.cls, item.sub_class = guess
                item.confidence = "geometry"
                named += 1
        return named

    def still_loose(indices):
        return [index for index in indices if items[index].cls in (None, "Other")]

    def current_loose():
        return [index for index, item in enumerate(items)
                if item.element_type in ("Furniture", "Decor")
                and item.cls in (None, "Other")]

    loose = current_loose()
    # The denominator `still_unnamed` is measured against, tracked alongside
    # `named_by_size` rather than re-read from `len(loose)` at the end.
    # Regrouping replaces some of these items with fewer, bigger ones, so a
    # plain item count taken after that point no longer means what it did
    # before it -- eight fragments becoming two chairs is not eight items
    # going unaccounted for, and reading the count only after the regroup
    # made `still_unnamed` go negative.
    total_candidates = len(loose)
    named_by_size = name_by_size(loose)

    # Repetition, over what is left. A shape with four congruent copies is
    # furniture whatever its size says, and the largest class of small
    # repeated shapes on an office floor is its chairs.
    classes, repeat_report = find_repeats(items, still_loose(loose))

    # Some of those classes are not a repeated small object; they are a
    # repeated sub-part of a bigger one that touched nothing closely enough
    # for the first grouping pass to join -- see `merge_fragment_classes`.
    # Regrouping changes which items exist, so everything measured against
    # the old `items` above has to be measured again against the new one.
    merged_items, fragment_report = merge_fragment_classes(items, classes)
    if fragment_report["regrouped"]:
        items[:] = merged_items
        total_candidates += fragment_report["into"] - fragment_report["regrouped"]
        loose = current_loose()
        named_by_size += name_by_size(loose)
        classes, repeat_report = find_repeats(items, still_loose(loose))

    named_by_repeat = 0
    repeated_unnamed = 0
    for members in classes.values():
        # The class shares one shape, so one measurement names all of it.
        first = items[members[0]]
        guess = tax.classify_by_size(*first.size())
        for index in members:
            item = items[index]
            if guess:
                item.element_type, item.cls, item.sub_class = guess
                item.confidence = "geometry"
                named_by_repeat += 1
            else:
                # Repeated and unnamed is still worth knowing. It is a piece
                # of furniture, and saying so beats leaving it structural.
                # Counted apart from the named ones: an item cannot be both
                # named by repetition and still unnamed, and reporting it as
                # both is how a figure stops meaning anything.
                item.element_type, item.cls, item.sub_class = \
                    "Furniture", "Other", None
                # Left `unknown` on purpose. Repetition established that this
                # is furniture and nothing more, so its class is still
                # `Other`. Marking it `geometry` would count it as identified
                # in the figure the UI reports, which would be a claim to
                # know what it is.
                repeated_unnamed += 1

    return {
        "named_by_size": named_by_size,
        "named_by_repeat": named_by_repeat,
        # Known to be furniture because it repeats, but of no known kind.
        # Not part of `still_unnamed`: those two are exclusive, so they add up.
        "repeated_unnamed": repeated_unnamed,
        "repeats": repeat_report,
        # Fragments of a bigger object, joined into that many fewer objects.
        # Zero on a drawing that never needed this pass.
        "fragments_regrouped": fragment_report["regrouped"],
        "fragments_into": fragment_report["into"],
        # By subtraction, so the four counts are exclusive by construction
        # and add up to the number of loose pieces there were. Counting this
        # one independently is what let an item be reported as both named by
        # repetition and still unnamed. Measured against `total_candidates`,
        # not `len(loose)`: `loose` is only whatever is left at the very end,
        # and regrouping means that is not the same population this function
        # started with.
        "still_unnamed": (total_candidates - named_by_size - named_by_repeat
                          - repeated_unnamed),
        "loose": total_candidates,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Stage 4: naming a room by what stands in it
# ─────────────────────────────────────────────────────────────────────────────
#
# `refine_by_space` in `fs_clean` corrects a table's class from the room it
# stands in. This is the same evidence read the other way: where a room has
# no label to read, what is in it says what it is.
#
# It only ever fills a blank. A room that found its own name keeps it, because
# a drawing that labelled a room has said more than a furniture count can.

# A room with this many desks is open plan rather than an office.
OPEN_PLAN_DESKS = 4
# Seats around one table before the room is a meeting room.
MEETING_SEATS = 5
# A room longer than this relative to its width is a corridor.
CORRIDOR_ASPECT = 3.0
# A room with one or two desks is an office only if it is small enough to be
# one. A 60 m2 room with two desks in it is a sparsely filled open plan.
MAX_OFFICE_M2 = 30.0
# An empty room this size or smaller is a store rather than an empty office.
MAX_STORE_M2 = 12.0

# The label confidences this pass may overwrite, and nothing else.
#
# `label` means the drawing named the room, and a name beats any inference
# from what happens to be standing in it. The rest are guesses of the same
# kind as this one, made with less to go on: `area` reads nothing but the
# floor area, `shape` nothing but the outline. What is in a room is better
# evidence than either, so those give way.
#
# Getting this list wrong is silent. It first read `(None, "unknown",
# "shape")`, which left out `area` -- and `area` is what an unlabelled room
# actually gets, so the whole pass never fired on the one drawing it was
# written for while every count still looked plausible.
OVERRIDABLE_CONFIDENCE = (None, "unknown", "shape", "area")


def classify_by_contents(spaces, furniture):
    """Name the unlabelled rooms from what stands in them. Returns a report.

    Runs after `assign_to_spaces`, which is what puts a `space_id` on each
    piece. Every name it sets is marked `geometry`, so the UI can separate a
    room the drawing named from one this worked out.
    """
    contents = defaultdict(lambda: {"desks": 0, "seats": 0, "tables": 0,
                                    "largest_table": 0.0})
    for piece in furniture:
        space_id = piece.get("space_id")
        if not space_id:
            continue
        sub = piece.get("sub_class") or ""
        # Only count a room as furnished when something in it actually
        # counts. Reading `contents[space_id]` first creates the bucket
        # whatever the piece turns out to be, so a room holding nothing but
        # unclassified fragments reported as furnished.
        if not (sub == "Desk" or piece.get("cls") in ("Chair", "Table")
                or tax.seats_for(sub)):
            continue
        bucket = contents[space_id]
        if sub == "Desk":
            bucket["desks"] += 1
        if piece.get("cls") == "Chair" or tax.seats_for(sub):
            bucket["seats"] += tax.seats_for(sub) or 1
        if piece.get("cls") == "Table":
            bucket["tables"] += 1
            bucket["largest_table"] = max(bucket["largest_table"],
                                          piece.get("area") or 0.0)

    named = 0
    for space in spaces:
        # Never overwrite a name the drawing gave. A label beats an inference
        # every time, and this pass has no way to know better.
        if space.get("label") or space.get("name"):
            continue
        if space.get("label_confidence") not in OVERRIDABLE_CONFIDENCE:
            continue

        bucket = contents.get(space["id"]) or {
            "desks": 0, "seats": 0, "tables": 0, "largest_table": 0.0}
        x0, y0, x1, y1 = fs_clean.bbox_of(space["rings"][0])
        long_side = max(x1 - x0, y1 - y0)
        short_side = max(min(x1 - x0, y1 - y0), 1e-9)
        area = space.get("area") or 0.0

        guess = None
        if long_side / short_side >= CORRIDOR_ASPECT:
            # Shape first for a corridor. A corridor holds stray furniture
            # and is still a corridor, and its shape is unmistakable.
            guess = ("Circulate", "Corridor")
        elif bucket["desks"] >= OPEN_PLAN_DESKS:
            guess = ("Work", "Open Workspace")
        elif bucket["seats"] >= MEETING_SEATS and bucket["tables"]:
            # Seats round a table and no desks is a meeting room. The desk
            # test has to come first, because a bench run of desks also has
            # seats round it and is not a meeting room.
            guess = ("Meet", "Meeting Room")
        elif bucket["desks"] and area <= MAX_OFFICE_M2:
            guess = ("Work", "Enclosed Workspace")
        elif bucket["desks"]:
            # Desks, but too much floor for an office. Sparsely filled open
            # plan, which is what a drawing whose furniture is half missing
            # looks like.
            guess = ("Work", "Open Workspace")
        elif bucket["seats"] >= 2 and bucket["tables"]:
            guess = ("Meet", "Huddle Room")
        elif not bucket["seats"] and not bucket["tables"] and area <= MAX_STORE_M2:
            guess = ("Support", "Storage")
        if not guess:
            continue

        space["cls"], space["sub_class"] = guess
        space["label_confidence"] = "geometry"
        named += 1

    return {"named_from_contents": named,
            "rooms_with_furniture": len(contents)}
