"""
Drawing-unit detection for arbitrary DXF files.
===============================================

A DXF stores bare numbers. The unit those numbers mean is not reliable.
`$INSUNITS` is often wrong, absent, or left at a converter default. A DWG to
DXF conversion frequently loses it. So this module treats the header as a weak
hint and decides from the geometry instead.

The method scores each candidate unit against real-world building sizes. A
floor plate is tens of metres across. An internal wall is about 100 mm thick.
A task chair is about 0.6 m wide. Only one candidate unit makes all three come
out right, so the correct scale wins by a wide margin.

Detecting one scale for the whole file matters. Applying a different scale per
layer, or per block, produces geometry that is individually the right size but
mutually misplaced. See `docs/` note in fs_clean for why that failure mode is
so hard to spot.

Public entry point: `detect_unit_scale(doc)` returns metres per drawing unit.
"""

from __future__ import annotations

import math
import re
from collections import Counter

# ─────────────────────────────────────────────────────────────────────────────
# Candidate units
# ─────────────────────────────────────────────────────────────────────────────

# Metres per drawing unit, for every unit a floor plan is realistically drawn
# in. Order is only for stable reporting.
CANDIDATES = {
    "mm": 0.001,
    "cm": 0.01,
    "in": 0.0254,
    "dm": 0.1,
    "ft": 0.3048,
    "m": 1.0,
}

# DXF `$INSUNITS` code to unit name. Used as a tie-break hint only.
INSUNITS_MAP = {
    1: "in",
    2: "ft",
    4: "mm",
    5: "cm",
    6: "m",
    14: "dm",
}


# ─────────────────────────────────────────────────────────────────────────────
# Setting the scale by hand
# ─────────────────────────────────────────────────────────────────────────────

# Detection reads the drawing's own geometry, and on most files it is right.
# Some files carry nothing to read. A DXF plotted from a PDF holds no blocks,
# no wall layer and no dimension text, so every measurement detection relies
# on is missing and the answer it settles on can be out by a factor of ten.
#
# A person reading the same drawing has no such trouble. They know a door is
# about 900 mm wide, so one measured dimension fixes the scale for the whole
# file. That is what these two functions are for. They are the backstop under
# every heuristic in this module: whatever detection cannot work out, someone
# can state in one measurement, and no code has to change per file.

# How close a hand measurement must land to a standard unit before it is
# treated as that unit. Two effects set the size of this window.
#
# When the drawing really is in a standard unit and detection merely picked
# the wrong one, the geometry is exact and the only error is in the size the
# user quoted. Calling a 914 mm door "0.9 m" is 1.6% out, so the window has
# to be wider than that to recover the exact unit.
#
# It must also stay narrow enough not to swallow a genuine custom scale. The
# nearest standard unit to a PDF plotted at 1 unit = 10 ft is metres, and
# that is a factor of 3 away, so real custom scales are nowhere near this.
UNIT_SNAP_FRAC = 0.02


def nearest_unit(scale):
    """Return (name, unit_scale, error) for the closest standard unit.

    The error is the size of the log of the ratio between the two, which for
    a close match is the ordinary relative error. Comparing this way is what
    makes "nearest" mean anything across candidates spanning a thousandfold.

    Two simpler forms are both wrong, and the second one is wrong subtly.

    An absolute difference calls every small scale millimetres, because every
    small scale is close to it in absolute terms.

    A one-sided relative difference, `abs(scale - unit) / unit`, is
    asymmetric: it charges an overshoot more than an undershoot. At 0.002 it
    makes centimetres the nearest unit, scoring an 80% undershoot better than
    the 100% overshoot from millimetres, when 0.002 is a factor of 2 from a
    millimetre and a factor of 5 from a centimetre. A ratio has no such bias.
    """
    best = None
    for name, unit_scale in CANDIDATES.items():
        error = abs(math.log(scale / unit_scale))
        if best is None or error < best[2]:
            best = (name, unit_scale, error)
    return best


def scale_from_measurement(drawing_distance, real_metres, snap=True):
    """Metres per drawing unit, from one dimension measured on the drawing.

    `drawing_distance` is the distance between two points in the drawing's
    own raw units. `real_metres` is what that distance is in the real world.
    Returns (scale, info).

    With `snap` left on, a result within `UNIT_SNAP_FRAC` of a standard unit
    is replaced by that unit exactly. The quoted real size is the imprecise
    half of this calculation, so a result 1% from inches means inches, and
    saying so gives a rounder scale than the arithmetic does.
    """
    try:
        drawing_distance = float(drawing_distance)
        real_metres = float(real_metres)
    except (TypeError, ValueError):
        raise ValueError(
            "Both the measured distance and the real size must be numbers.")
    if not math.isfinite(drawing_distance) or drawing_distance <= 0:
        raise ValueError(
            "The measured distance must be more than zero. Click two "
            "separate points on the drawing.")
    if not math.isfinite(real_metres) or real_metres <= 0:
        raise ValueError(
            "The real size must be more than zero. Give it in metres, so a "
            "900 mm door is 0.9.")

    raw = real_metres / drawing_distance
    name, unit_scale, error = nearest_unit(raw)
    snapped = bool(snap and error <= UNIT_SNAP_FRAC)
    return (unit_scale if snapped else raw), {
        "raw_scale": raw,
        "scale": unit_scale if snapped else raw,
        "unit": name if snapped else "custom",
        "nearest_unit": name,
        "nearest_error": round(error, 5),
        "snapped": snapped,
        "drawing_distance": drawing_distance,
        "real_metres": real_metres,
    }


def rescale_from_metres(current_scale, measured_metres, real_metres,
                        snap=True):
    """The same, when the distance was measured on an already-scaled plan.

    The app measures on the canvas, whose coordinates are raw drawing units
    already multiplied by the scale in force. So the user is not reporting a
    raw distance, they are reporting that a distance the plan currently calls
    `measured_metres` is really `real_metres`. Undo the current scale to get
    back to drawing units, then measure from those.

    Keeping this here rather than in the page means the arithmetic is tested.
    """
    try:
        current_scale = float(current_scale)
    except (TypeError, ValueError):
        raise ValueError("The current scale must be a number.")
    if not math.isfinite(current_scale) or current_scale <= 0:
        raise ValueError("The current scale must be more than zero.")
    try:
        measured_metres = float(measured_metres)
    except (TypeError, ValueError):
        raise ValueError("The measured distance must be a number.")
    if not math.isfinite(measured_metres) or measured_metres <= 0:
        raise ValueError(
            "The measured distance must be more than zero. Click two "
            "separate points on the drawing.")
    return scale_from_measurement(
        measured_metres / current_scale, real_metres, snap=snap)

# ─────────────────────────────────────────────────────────────────────────────
# Plausible real-world ranges, in metres
# ─────────────────────────────────────────────────────────────────────────────

# A single floor plate. Below 6 m is a room, not a floor. Above 500 m is a
# whole site or a coordinate-system artefact.
FLOOR_MIN, FLOOR_MAX = 6.0, 500.0
# Most office floors land here. Used to prefer the best of several valid fits.
FLOOR_IDEAL_LO, FLOOR_IDEAL_HI = 15.0, 150.0

# Internal partitions and structural walls, measured as the short side of a
# wall band. 60 mm is a thin glazed partition, 500 mm a core wall.
WALL_MIN, WALL_MAX = 0.06, 0.5
WALL_IDEAL_LO, WALL_IDEAL_HI = 0.08, 0.35

# The long side of a furniture block in plan. A stool is 0.4 m, a conference
# table 4 m.
FURN_MIN, FURN_MAX = 0.3, 4.5
FURN_IDEAL_LO, FURN_IDEAL_HI = 0.45, 2.5


def _fit_score(value, hard_lo, hard_hi, soft_lo, soft_hi):
    """Score how well one measurement fits a real-world range, from 0 to 1.

    A value inside the soft range scores 1. Outside the hard range scores 0.
    Between the two the score falls off smoothly, so a near miss still counts
    for something and no single measurement can veto a unit on its own.
    """
    if value is None or value <= 0:
        return 0.0
    if soft_lo <= value <= soft_hi:
        return 1.0
    if value < soft_lo:
        if value <= hard_lo:
            return 0.0
        return (value - hard_lo) / (soft_lo - hard_lo)
    if value >= hard_hi:
        return 0.0
    return (hard_hi - value) / (hard_hi - soft_hi)


# ─────────────────────────────────────────────────────────────────────────────
# Evidence gathering, in raw drawing units
# ─────────────────────────────────────────────────────────────────────────────

def _polyline_points(entity):
    """Return an entity's 2D vertices, or an empty list if it has none."""
    kind = entity.dxftype()
    try:
        if kind == "LWPOLYLINE":
            return [(p[0], p[1]) for p in entity.get_points()]
        if kind == "POLYLINE":
            return [(v.dxf.location[0], v.dxf.location[1]) for v in entity.vertices]
        if kind == "LINE":
            s, e = entity.dxf.start, entity.dxf.end
            return [(s[0], s[1]), (e[0], e[1])]
        if kind == "CIRCLE":
            c, r = entity.dxf.center, entity.dxf.radius
            return [(c[0] - r, c[1] - r), (c[0] + r, c[1] + r)]
    except Exception:
        return []
    return []


def _bbox(points):
    """Return (min_x, min_y, max_x, max_y) for a list of points."""
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def _mode_of(values, bucket_ratio=0.05):
    """Return the most common value in a list, grouped into relative buckets.

    A plain median is pulled around by the many tiny door-stop and mullion
    rectangles that share a wall layer. The mode of log-spaced buckets picks
    the one thickness the drawing actually repeats.
    """
    values = [v for v in values if v > 0]
    if not values:
        return None
    counts = Counter()
    for v in values:
        counts[round(math.log(v) / bucket_ratio)] += 1
    best_bucket, _ = counts.most_common(1)[0]
    members = [v for v in values
               if round(math.log(v) / bucket_ratio) == best_bucket]
    members.sort()
    return members[len(members) // 2]


# ─────────────────────────────────────────────────────────────────────────────
# Floor-plate location
# ─────────────────────────────────────────────────────────────────────────────

# A drawing routinely carries content that is not on the floor: a mirrored
# backup copy, a detail block parked off to one side, a stray INSERT the DWG
# conversion misplaced. Percentile trimming does not remove it, because the
# strays can be several percent of the vertices and all on one side.
#
# What does separate them is the empty corridor between. Off-plan content
# sits across a gap far wider than any gap inside the floor plate itself, so
# splitting each axis at its wide gaps and keeping the busiest run isolates
# the floor. Working one axis at a time keeps this free of any cell size or
# resolution, which matters because it has to run before the drawing's unit
# is known.

# A gap must exceed this fraction of the axis span to count as a separation.
# An open floor plate never contains an empty band a tenth of its own width;
# anything that wide means two separate pieces of content.
GAP_FRACTION = 0.10
# Keep a run only if it holds at least this fraction of the coverage. Guards
# against splitting a floor into slivers when a drawing is unusually sparse.
RUN_MIN_SHARE = 0.05
# Bins per axis for the coverage histogram. Fine enough to resolve the empty
# margin beside a floor, coarse enough to stay cheap on a large drawing.
OCCUPANCY_BINS = 400
# A bin counts as occupied at this fraction of the busiest bin's coverage.
# This is what stops one stray entity that spans a gap from welding the
# floor to a mirrored copy parked next to it.
OCCUPANCY_FRACTION = 0.01


def dominant_interval(intervals, gap_fraction=GAP_FRACTION):
    """Return (low, high, share) for the busiest stretch of `intervals`.

    Each interval is one entity's extent on one axis. The axis is divided
    into bins, each entity marks every bin it covers, and the busiest run of
    well-covered bins wins.

    Two failure modes rule out the simpler approaches.

    Clustering vertices does not work: a long wall drawn as a rectangle has
    only four corners, so a vertex cloud shows a gap the length of the wall
    and splits a sparse floor down the middle.

    Merging overlapping intervals does not work either, because merging is
    transitive. One stray entity that happens to span from the floor to a
    mirrored copy parked beside it welds the two into a single group, and
    the plan then measures twice its real width.

    Counting coverage per bin avoids both. A wall covers every bin along its
    length, so no gap appears inside it. A lone entity bridging a gap leaves
    only itself in the bins between, far below the floor's own coverage, so
    a relative threshold treats those bins as empty and the split holds.
    """
    if not intervals:
        return None
    lo_all = min(i[0] for i in intervals)
    hi_all = max(i[1] for i in intervals)
    span = hi_all - lo_all
    if span <= 0:
        return lo_all, hi_all, 1.0

    bins = OCCUPANCY_BINS
    width = span / bins
    counts = [0] * bins
    for low, high in intervals:
        first = min(bins - 1, max(0, int((low - lo_all) / width)))
        last = min(bins - 1, max(0, int((high - lo_all) / width)))
        for b in range(first, last + 1):
            counts[b] += 1

    peak = max(counts)
    if peak <= 0:
        return lo_all, hi_all, 1.0
    # Relative, with a floor of one entity. On a busy plan this excludes a
    # single bridging stray; on a sparse one it still accepts a lone wall.
    threshold = max(1, peak * OCCUPANCY_FRACTION)
    gap_bins = max(1, int(bins * gap_fraction))

    runs = []
    start = None
    empty = 0
    for b in range(bins):
        if counts[b] >= threshold:
            if start is None:
                start = b
            empty = 0
        elif start is not None:
            empty += 1
            if empty > gap_bins:
                runs.append((start, b - empty))
                start = None
                empty = 0
    if start is not None:
        runs.append((start, bins - 1))
    if not runs:
        return lo_all, hi_all, 1.0

    weights = [sum(counts[a:b + 1]) for a, b in runs]
    best_index = weights.index(max(weights))
    a, b = runs[best_index]
    share = weights[best_index] / max(1, sum(counts))
    if share < RUN_MIN_SHARE:
        return lo_all, hi_all, 1.0
    return lo_all + a * width, lo_all + (b + 1) * width, share


def floor_cluster(boxes, gap_fraction=GAP_FRACTION):
    """Locate the floor plate, ignoring off-plan content.

    `boxes` is a list of (min_x, min_y, max_x, max_y) extents, one per
    entity. A bare (x, y) point is accepted too and treated as a zero-sized
    box, but per-entity extents give a far better answer on a sparse
    drawing.

    Returns a dict with the plate's `bbox` and the fraction of boxes it
    `covers`, or None when there is too little to judge.

    Runs twice. The first pass finds the busy interval on each axis
    independently. The second re-measures each axis using only what the
    other axis kept, which sharpens the result when strays sit diagonally
    off the floor.
    """
    if not boxes or len(boxes) < 4:
        return None

    normalised = []
    for box in boxes:
        if len(box) >= 4:
            normalised.append((box[0], box[1], box[2], box[3]))
        else:
            normalised.append((box[0], box[1], box[0], box[1]))

    kept = normalised
    bbox = None
    for _ in range(2):
        x_lo, x_hi, _ = dominant_interval([(b[0], b[2]) for b in kept], gap_fraction)
        y_lo, y_hi, _ = dominant_interval([(b[1], b[3]) for b in kept], gap_fraction)
        bbox = (x_lo, y_lo, x_hi, y_hi)
        # An entity counts as on the plate when its centre is on it, so a
        # wall running along the boundary is not thrown out by rounding.
        nxt = [b for b in kept
               if x_lo <= (b[0] + b[2]) / 2 <= x_hi
               and y_lo <= (b[1] + b[3]) / 2 <= y_hi]
        if len(nxt) == len(kept) or len(nxt) < 4:
            break
        kept = nxt

    if bbox is None or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        return None

    # Snap the box to the entities that are actually on the plate, rather
    # than leaving it on the histogram's bin edges.
    on_plate = [b for b in normalised
                if bbox[0] <= (b[0] + b[2]) / 2 <= bbox[2]
                and bbox[1] <= (b[1] + b[3]) / 2 <= bbox[3]]
    if not on_plate:
        return {"bbox": bbox, "covers": 0.0}
    tight = (min(b[0] for b in on_plate), min(b[1] for b in on_plate),
             max(b[2] for b in on_plate), max(b[3] for b in on_plate))
    # Never let one wide stray stretch the box back out past the histogram.
    bbox = (max(bbox[0], tight[0]), max(bbox[1], tight[1]),
            min(bbox[2], tight[2]), min(bbox[3], tight[3]))
    return {"bbox": bbox, "covers": len(on_plate) / len(normalised)}


# A wall band must be at least this fraction of the plan's extent long, and
# no thicker than this fraction of it. Both bounds are relative, so they hold
# whatever unit the file turns out to be in. Without the length bound the
# thickness mode latches onto the hundreds of tiny door-stop and mullion
# profile rectangles that share the architecture layer.
BAND_MIN_LONG_FRAC = 0.02
BAND_MAX_SHORT_FRAC = 0.05
BAND_MIN_ASPECT = 4.0
# Layers whose bands are walls. Measure the thickness from these alone when
# the file has them, because a drawing where the furniture is exploded into
# modelspace carries tens of thousands of thin desk-screen and panel bands
# against a few hundred wall bands, and the mode of everything together is
# then the screen thickness, not the wall. Glazing counts as structure but
# not as a wall: a curtain-wall pane line is far thinner than a partition.
WALL_LAYER = re.compile(r"wall|partition|masonry|\bcore\b|\barch",
                        re.IGNORECASE)
# Below this many wall-layer bands the mode is a coin toss, so fall back to
# measuring every closed band in the drawing.
WALL_BAND_MIN_SAMPLES = 6
# Below this many measured line-pair gaps the same applies.
WALL_PAIR_MIN_SAMPLES = 8
# Forward neighbours each segment is compared with. Bounds the cost on a
# drawing that stacks hundreds of segments at the same coordinate.
PAIR_SCAN_LIMIT = 40
# How far two lines must run alongside each other to count as a wall's two
# faces, as a fraction of the plan's extent. An aspect test alone is not
# enough, because it scales with the gap: at four times the gap, a 0.5 inch
# sliver qualifies on two inches of overlap. A drawing has hundreds of those
# slivers, from wall end caps and door stops, and they outvote the real
# walls. Requiring a plan-relative run length as well leaves only lines that
# genuinely track each other. Measured on the files in DXF/: any value from
# 0.002 to 0.007 gives the same answer on all of them, so this sits in the
# middle of that plateau rather than on an edge of it.
PAIR_MIN_OVERLAP_FRAC = 0.004
# A furniture block longer than this fraction of the plan is not furniture.
# It is a mis-scaled INSERT or a whole-floor underlay drawn as a block.
FURN_MAX_EXTENT_FRAC = 0.3


def _axis_segments(points):
    """Axis-aligned pieces of a path, as (axis, position, low, high).

    Axis 0 is a horizontal piece, held at a constant y. Axis 1 is vertical.
    Anything slanted is skipped: a wall thickness is measured across a wall,
    and a diagonal pair needs a projection this does not attempt.
    """
    out = []
    for index in range(len(points) - 1):
        (x0, y0), (x1, y1) = points[index], points[index + 1]
        if y0 == y1 and x0 != x1:
            out.append((0, y0, min(x0, x1), max(x0, x1)))
        elif x0 == x1 and y0 != y1:
            out.append((1, x0, min(y0, y1), max(y0, y1)))
    return out


def pair_gaps(segments, max_gap, min_aspect=BAND_MIN_ASPECT, min_overlap=0.0):
    """Gaps between the parallel faces of a wall drawn as two lines.

    A great many drawings never draw a wall as a closed band: they draw its
    two faces and stop. There is then nothing band-shaped to measure, and
    the thickness has to come from the distance between the pair.

    Two segments are a pair when they share an axis, sit less than `max_gap`
    apart, and run alongside each other for at least `min_aspect` times that
    gap and at least `min_overlap` in absolute terms. Both overlap tests are
    needed. The relative one separates a wall from a door frame. The
    absolute one separates a wall from an end cap, which the relative test
    alone waves through because its own required overlap shrinks with it.
    """
    gaps = []
    # Two lines closer together than this are a coincident pair, not a wall:
    # a duplicate, or a face drawn twice by a DWG conversion. Left in, a few
    # dozen of them can win the mode and report a wall zero units thick.
    floor = max_gap * 1e-4
    for axis in (0, 1):
        same = sorted((s for s in segments if s[0] == axis),
                      key=lambda s: s[1])
        for index, (_, position, low, high) in enumerate(same):
            for other in same[index + 1:index + 1 + PAIR_SCAN_LIMIT]:
                gap = other[1] - position
                if gap <= floor:
                    continue
                if gap > max_gap:
                    break
                overlap = min(high, other[3]) - max(low, other[2])
                if overlap >= max(min_aspect * gap, min_overlap):
                    gaps.append(gap)
    return gaps


# Exploding a block reference is expensive, and on a file with deeply nested
# blocks one call fans out into thousands. Detection does not need every
# instance: extent, wall thickness and furniture size are all population
# statistics, and the loose geometry that is not in any block still anchors
# the plan's extent. Sampling evenly across the drawing keeps the answer and
# drops most of the cost.
MAX_INSERT_SAMPLES = 500


def _collect_evidence(doc, max_insert_samples=MAX_INSERT_SAMPLES):
    """Measure the drawing in raw units.

    Returns a dict with `extent`, `wall_thickness` and `furniture_size`. Any
    value may be None when the file has nothing to measure it from.

    Runs in two passes. The first gathers candidate shapes and the plan
    extent. The second filters those shapes against the extent, because
    "is this band long enough to be a wall" only has meaning relative to the
    size of the plan it sits in.
    """
    msp = doc.modelspace()

    all_points = []
    entity_boxes = []   # one extent per entity, for locating the floor plate
    bands = []          # (short_side, long_side) for every closed thin shape
    wall_bands = []     # the same, for thin shapes on a wall layer
    wall_segments = []  # axis-aligned pieces of wall-layer line work
    block_sizes = []    # long side of every placed block

    # Stride evenly through the block references rather than taking the
    # first N, so the sample covers the whole drawing and not one corner.
    entities = list(msp)
    insert_indices = [i for i, e in enumerate(entities) if e.dxftype() == "INSERT"]
    if max_insert_samples and len(insert_indices) > max_insert_samples:
        stride = len(insert_indices) / max_insert_samples
        sampled = {insert_indices[min(len(insert_indices) - 1, int(i * stride))]
                   for i in range(max_insert_samples)}
    else:
        sampled = set(insert_indices)

    for index, entity in enumerate(entities):
        kind = entity.dxftype()

        if kind == "INSERT":
            if index not in sampled:
                continue
            # Measure the block as placed, so the INSERT scale is included.
            # A block's own scale is a legitimate unit conversion: a
            # millimetre-authored chair placed into an inch drawing carries
            # xscale 1/25.4. Applying it as authored is correct.
            try:
                parts = []
                for sub in entity.virtual_entities():
                    sub_points = _polyline_points(sub)
                    if sub_points:
                        parts.append(sub_points)
            except Exception:
                continue
            if not parts:
                continue

            points = [p for part in parts for p in part]
            all_points.extend(points)

            # One box per piece of the block, not one box around the whole
            # block. Some blocks in a DWG conversion hold a mirrored copy of
            # their own geometry, so the block's overall box spans from the
            # floor to the copy and reads as a single entity bridging the
            # empty gap between them. Its pieces do not.
            for part in parts:
                entity_boxes.append(_bbox(part))

            x0, y0, x1, y1 = _bbox(points)
            long_side = max(x1 - x0, y1 - y0)
            if long_side > 0:
                block_sizes.append(long_side)
            continue

        points = _polyline_points(entity)
        if not points:
            continue
        all_points.extend(points)
        entity_boxes.append(_bbox(points))

        # A closed, elongated, thin polygon is a wall band. Its short side is
        # the wall thickness. This is the single most discriminating
        # measurement available, because wall thickness varies far less
        # between buildings than floor size does.
        if WALL_LAYER.search(entity.dxf.layer or ""):
            wall_segments.extend(_axis_segments(points))

        if kind in ("LWPOLYLINE", "POLYLINE"):
            x0, y0, x1, y1 = _bbox(points)
            short, long_ = min(x1 - x0, y1 - y0), max(x1 - x0, y1 - y0)
            if short > 0 and long_ / short >= BAND_MIN_ASPECT:
                if getattr(entity, "closed", False):
                    bands.append((short, long_))
                # An open polyline on a wall layer is still wall evidence:
                # plenty of drawings trace a wall's two faces and leave the
                # ends unjoined. The aspect test above already guarantees
                # the shape is a thin strip and not, say, an L of corridor
                # walls, because an L's own bounding box is nearly square.
                if WALL_LAYER.search(entity.dxf.layer or ""):
                    wall_bands.append((short, long_))

    cluster = floor_cluster(entity_boxes)
    if cluster:
        bx0, by0, bx1, by1 = cluster["bbox"]
        extent = max(bx1 - bx0, by1 - by0)
    else:
        extent = None
        if all_points:
            x0, y0, x1, y1 = _bbox(all_points)
            extent = max(x1 - x0, y1 - y0) or None

    wall_thickness = None
    furniture_size = None
    if extent:
        def in_range(source):
            return [s for s, l in source
                    if l >= BAND_MIN_LONG_FRAC * extent
                    and s <= BAND_MAX_SHORT_FRAC * extent]

        on_wall_layers = in_range(wall_bands)
        gaps = pair_gaps(wall_segments, BAND_MAX_SHORT_FRAC * extent,
                         min_overlap=PAIR_MIN_OVERLAP_FRAC * extent)
        if len(on_wall_layers) >= WALL_BAND_MIN_SAMPLES:
            wall_thickness = _mode_of(on_wall_layers)
        elif len(gaps) >= WALL_PAIR_MIN_SAMPLES:
            # Nothing band-shaped on a wall layer, so measure across the
            # pairs of lines the drawing used instead.
            wall_thickness = _mode_of(gaps)
        else:
            wall_thickness = _mode_of(in_range(bands))
        furniture_size = _mode_of(
            [s for s in block_sizes if s <= FURN_MAX_EXTENT_FRAC * extent])

    return {
        "extent": extent,
        "wall_thickness": wall_thickness,
        "furniture_size": furniture_size,
        "_cluster": cluster,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Scoring
# ─────────────────────────────────────────────────────────────────────────────

# Wall thickness is weighted highest because it is the most constrained
# measurement in a building. Floor extent is weakest because floor plates
# genuinely vary from 15 m to 150 m across.
WEIGHTS = {"wall_thickness": 3.0, "furniture_size": 2.0, "extent": 1.5}
# How much a matching `$INSUNITS` header is worth. Enough to break a tie
# between two units that fit the geometry equally well, never enough to
# override geometry that clearly disagrees.
HINT_BONUS = 0.15
# The header hint is ignored below this geometric score. A DWG-converted file
# routinely declares millimetres while holding inches, and without this gate
# that one wrong header outvotes every real measurement in the drawing.
HINT_MIN_SCORE = 0.35


def score_candidate(scale, evidence):
    """Score one candidate scale against the measured evidence.

    Returns (score, detail) where score is 0 to 1 and detail records the
    real-world size each measurement implies at this scale.
    """
    detail = {}
    total = 0.0
    weight_used = 0.0

    checks = (
        ("extent", FLOOR_MIN, FLOOR_MAX, FLOOR_IDEAL_LO, FLOOR_IDEAL_HI),
        ("wall_thickness", WALL_MIN, WALL_MAX, WALL_IDEAL_LO, WALL_IDEAL_HI),
        ("furniture_size", FURN_MIN, FURN_MAX, FURN_IDEAL_LO, FURN_IDEAL_HI),
    )

    for key, hard_lo, hard_hi, soft_lo, soft_hi in checks:
        raw = evidence.get(key)
        if raw is None:
            continue
        metres = raw * scale
        sub = _fit_score(metres, hard_lo, hard_hi, soft_lo, soft_hi)
        detail[key] = {"metres": metres, "score": sub}
        total += sub * WEIGHTS[key]
        weight_used += WEIGHTS[key]

    if weight_used == 0:
        return 0.0, detail
    return total / weight_used, detail


def detect_unit_scale(doc, override=None, measure=True):
    """Return (metres_per_unit, report) for a DXF document.

    `override` forces a scale. With `measure` left on, the geometry is still
    measured so the report can say whether the forced scale agrees with it.
    Turn `measure` off to skip that: measuring means exploding every block
    reference, which is the most expensive pass in the whole pipeline, and a
    caller re-using a scale already measured on the same file has no reason
    to pay for it twice.
    """
    if override and not measure:
        chosen_scale = float(override)
        chosen_unit = next((n for n, s in CANDIDATES.items()
                            if abs(s - chosen_scale) < 1e-9), "custom")
        return chosen_scale, {
            "unit": chosen_unit,
            # A scale set by measuring a PDF plot is not a standard unit, so
            # naming the nearest one gives the reader a sense of it.
            "nearest_unit": nearest_unit(chosen_scale)[0],
            "scale": chosen_scale,
            "source": "given",
            "wall_thickness_m": None,
            "confidence": None,
            "margin": None,
            "header_insunits": None,
            "header_unit": None,
            "header_agrees": None,
            "evidence_raw": {},
            "floor_cluster": None,
            "implied_metres": {},
            "ranking": [],
        }

    evidence = _collect_evidence(doc)

    try:
        insunits = int(doc.header.get("$INSUNITS", 0) or 0)
    except Exception:
        insunits = 0
    hint = INSUNITS_MAP.get(insunits)

    results = []
    for name, scale in CANDIDATES.items():
        score, detail = score_candidate(scale, evidence)
        geometry_score = score
        if hint == name and geometry_score >= HINT_MIN_SCORE:
            score += HINT_BONUS
        results.append({
            "unit": name,
            "scale": scale,
            "score": score,
            "geometry_score": geometry_score,
            "detail": detail,
            "header_hint": hint == name,
        })
    results.sort(key=lambda r: -r["score"])

    best = results[0]
    runner_up = results[1] if len(results) > 1 else None

    if override:
        chosen_scale = float(override)
        chosen_unit = next((n for n, s in CANDIDATES.items()
                            if abs(s - chosen_scale) < 1e-9), "custom")
        # Report whether the supplied scale is the one the geometry points
        # to. A caller passing the value measured earlier on the same file
        # should not be told the answer came from nowhere.
        source = "given" if chosen_unit == best["unit"] else "override"
    elif best["score"] <= 0.0:
        # Nothing measurable, or nothing plausible. Fall back to the header,
        # then to metres. Better to import something than to refuse.
        chosen_unit = hint or "m"
        chosen_scale = CANDIDATES.get(chosen_unit, 1.0)
        source = "header" if hint else "fallback"
    else:
        chosen_unit = best["unit"]
        chosen_scale = best["scale"]
        source = "geometry"

    # The wall thickness measured here is the most reliable one available, so
    # it is published rather than left for a caller to re-derive. It comes
    # from the mode of the drawing's own wall bands, filtered relative to the
    # plan size, which a simpler measurement gets wrong: rounding a mixed
    # population of bands and door-stop slivers lands on a sliver.
    raw_thickness = evidence.get("wall_thickness")
    wall_thickness_m = (float(raw_thickness) * chosen_scale
                        if raw_thickness else None)

    report = {
        "unit": chosen_unit,
        "nearest_unit": nearest_unit(chosen_scale)[0],
        "scale": chosen_scale,
        "source": source,
        "wall_thickness_m": (round(wall_thickness_m, 4)
                             if wall_thickness_m else None),
        "confidence": round(best["score"], 3),
        "margin": round(best["score"] - runner_up["score"], 3) if runner_up else None,
        "header_insunits": insunits,
        "header_unit": hint,
        "header_agrees": hint == chosen_unit,
        # Plain floats throughout. These reports cross into JavaScript, and a
        # numpy scalar is not JSON-serialisable.
        "evidence_raw": {k: round(float(v), 4)
                         for k, v in evidence.items()
                         if not k.startswith("_") and v is not None},
        "floor_cluster": evidence.get("_cluster"),
        "implied_metres": {
            k: round(float(v["metres"]), 4)
            for k, v in (best["detail"] or {}).items()
        },
        "ranking": [{"unit": r["unit"], "score": round(r["score"], 3)}
                    for r in results],
    }
    return chosen_scale, report
