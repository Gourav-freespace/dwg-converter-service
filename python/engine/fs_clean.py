"""
Clean floor-plan model from an arbitrary DXF.
=============================================

Turns a DXF into one coherent, classified floor plan:

    walls      one solid wall mass, reconstructed by raster closing
    doors      door swings and leaves, kept as line work
    windows    window and mullion bands
    openings   the gap each door leaves in the wall
    spaces     rooms, from a space overlay if present or traced if not
    furniture  one element per placed block, individually classified

Everything comes out in metres, positioned consistently, with the plan
centred on the origin.

The one rule that matters
------------------------
A DXF is drawn in one unit. Convert the whole file by one factor and nothing
moves relative to anything else. Convert a layer, or a block, by its own
factor and every shape is individually the right size while the plan itself
is destroyed: rooms keep their dimensions but sit tens of times too far
apart. That failure passes every count and size check, which is exactly why
it is worth stating plainly. `fs_units` picks the single factor. Nothing
downstream of it may apply another.

Block-level INSERT scales are not an exception to that rule. A millimetre
block placed into an inch drawing legitimately carries xscale 1/25.4, and
applying it as authored is what puts the block at its true size. Those are
resolved by ezdxf during flattening, before the file's own factor applies.
"""

from __future__ import annotations

import math
import uuid
from collections import Counter, defaultdict

import numpy as np

import dxf_reader
import fs_raster as raster
import fs_taxonomy as tax
import fs_units

try:
    from ezdxf.path import make_path
except Exception:                                    # pragma: no cover
    make_path = None

try:
    from ezdxf.path import from_hatch
except Exception:                                    # pragma: no cover
    from_hatch = None


# ─────────────────────────────────────────────────────────────────────────────
# Tuning
# ─────────────────────────────────────────────────────────────────────────────

# Curve flattening tolerance, in metres. 8 mm keeps a door swing smooth and a
# tessellated circle recognisable without inflating the vertex count.
CURVE_TOLERANCE = 0.008
# Raster cell as a fraction of the detected wall thickness. A quarter of a
# wall resolves the wall itself and every gap that matters near it, and keeps
# the traced outline close to the drawn line.
CELL_PER_WALL = 1 / 4
# Hard bounds on the cell, so a freak wall thickness cannot ruin the raster.
CELL_MIN, CELL_MAX = 0.010, 0.060
# Ignore a traced wall fragment below this area. Real walls are longer.
MIN_WALL_FRAGMENT_M2 = 0.02
# Ignore a piece of floor slab below this area, in square metres. The
# footprint is a raster, so its edge carries specks.
MIN_FLOOR_PIECE_M2 = 1.0
# Smallest door or window a layer alone may declare, in metres. A door layer
# also carries frame ticks and stop marks a few centimetres long, and a
# glazing layer carries a joint mark at every pane edge. On one real file
# those outnumber the doors two to one and the panes three to two. A block
# that names itself a door or a mullion is exempt: the drawing has said what
# it is, and a mullion is legitimately 100 mm wide.
MIN_LAYER_FEATURE_M = 0.3
# Nested block recursion cap. Real files nest three or four deep.
MAX_BLOCK_DEPTH = 8

# A traced room must be at least this many square metres. Below this it is a
# duct, a wall pocket, or a gap between two partitions.
MIN_ROOM_AREA = 1.2
# ... and no more than this, unless it is the single largest region. A sparse
# wall network lets one region swallow a whole floor.
MAX_ROOM_AREA = 2000.0
# A doorway, in metres. Used as the largest gap in a wall that cannot be a
# room, so it is what a wall gap has to be closed by before the rooms are
# traced. Measured from the drawing's own doors where it has any; these
# bounds only keep a mis-measured one from closing a corridor or nothing.
DOORWAY_M = 0.9
DOORWAY_MIN_M, DOORWAY_MAX_M = 0.7, 1.3
# Ignore any overlay polygon smaller than this. Serraview draws zero-width
# marker polylines that would otherwise become spaces.
MIN_SPACE_AREA = 0.4
# A residual open region below this many square metres is a leftover sliver
# between a wall and a space edge, not a place anyone stands.
MIN_OPEN_AREA = 4.0
# How many labels one augmenting search may displace. Chains along a row of
# desks are a few links long; this only exists to bound the stack.
MAX_AUGMENT_DEPTH = 64
# How far outside a space polygon a label may sit and still belong to it, in
# metres. A text anchor for a desk-sized polygon commonly lands just past the
# edge; measured worst case on the reference file is 0.68 m.
LABEL_SNAP_M = 1.0

# How far outside the detected floor plate an entity may sit before it is
# treated as off-plan, as a fraction of the plate's larger side.
OFF_PLAN_MARGIN = 0.04

# Two loose fragments closer than this, in metres, are parts of one object.
# 20 mm is smaller than the gap a draughtsman leaves between two pieces of
# furniture and larger than the rounding error in an exploded arc chain.
LOOSE_GROUP_GAP = 0.02
# A group of loose fragments wider than this, in metres, is not one object.
# It is a run that has welded together, so its members stay separate.
LOOSE_GROUP_MAX_SPAN = 12.0

# An overlay polygon covering at least this fraction of the plan's bounding
# box is the floor's own outline, not a room in it. Serraview exports one as
# SV-ZONE. Left in the space list it becomes a single 1600 square metre
# "room" that swallows the whole floor.
FLOOR_ZONE_AREA_FRAC = 0.4
# Gaps up to this many metres are bridged when deriving the floor footprint.
# A glazed facade is drawn as separate mullions with nothing between them, so
# the bridge has to reach across a full glazing panel.
FOOTPRINT_BRIDGE_M = 1.2
# The building envelope, as a multiple of the internal wall thickness. The
# drawing does not state a facade thickness, so this is an estimate, applied
# only when no perimeter wall was drawn.
PERIMETER_FACTOR = 2.0


# ─────────────────────────────────────────────────────────────────────────────
# Geometry helpers
# ─────────────────────────────────────────────────────────────────────────────

def ring_area(ring):
    """Absolute area of a closed ring."""
    total = 0.0
    for i in range(len(ring) - 1):
        total += ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1]
    return abs(total) / 2.0


def signed_area(ring):
    total = 0.0
    for i in range(len(ring) - 1):
        total += ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1]
    return total / 2.0


def ring_centroid(ring):
    """Area centroid of a closed ring, falling back to the vertex mean."""
    area = signed_area(ring)
    if abs(area) < 1e-12:
        n = max(1, len(ring))
        return [sum(p[0] for p in ring) / n, sum(p[1] for p in ring) / n]
    cx = cy = 0.0
    for i in range(len(ring) - 1):
        cross = ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1]
        cx += (ring[i][0] + ring[i + 1][0]) * cross
        cy += (ring[i][1] + ring[i + 1][1]) * cross
    return [cx / (6 * area), cy / (6 * area)]


def min_area_rect(points):
    """The smallest rectangle enclosing `points`.

    Returns (long, short, radians, (centre_x, centre_y)). The angle is that of
    the long side, and the centre is in world coordinates, so the four corners
    can be rebuilt from it and a run can be sliced along its own axis rather
    than the world's.

    A desk is 1.6 by 0.8 m at any angle. Its **axis-aligned** box is not: turn
    that desk 31 degrees and the box becomes 1.79 by 1.51 m, bigger on both
    sides at once, and every size rule that knows what a desk measures stops
    matching it.

    That is not a hypothetical. A real drawing of a wedge-shaped building sits
    at 31 degrees to the world axes, and its whole furniture schedule failed
    to classify: 184 desks measured 1.47 by 1.34 m instead of 1.20 by 0.85 m,
    407 pieces in all, and the plan reported 164 desks against 999 chairs.

    Rotating calipers over the convex hull. The smallest enclosing rectangle
    always has a side flush with a hull edge, so trying each edge in turn
    finds it. The hull is small for a piece of furniture, so the cost is not
    worth avoiding.
    """
    hull = convex_hull(points)
    if not hull or len(hull) < 4:
        return None
    best = None
    for index in range(len(hull) - 1):
        (ax, ay), (bx, by) = hull[index], hull[index + 1]
        angle = math.atan2(by - ay, bx - ax)
        cos, sin = math.cos(-angle), math.sin(-angle)
        xs = [x * cos - y * sin for x, y in hull]
        ys = [x * sin + y * cos for x, y in hull]
        lo_x, hi_x = min(xs), max(xs)
        lo_y, hi_y = min(ys), max(ys)
        width, height = hi_x - lo_x, hi_y - lo_y
        area = width * height
        if best is None or area < best[0]:
            best = (area, width, height, angle,
                    (lo_x + hi_x) / 2.0, (lo_y + hi_y) / 2.0)
    if best is None:
        return None
    _, width, height, angle, mid_x, mid_y = best
    # The centre back in world coordinates, by undoing the rotation used to
    # measure it.
    cos, sin = math.cos(angle), math.sin(angle)
    centre = (mid_x * cos - mid_y * sin, mid_x * sin + mid_y * cos)
    if height > width:
        # Report the long side first, so the angle is always the long axis.
        return height, width, angle + math.pi / 2.0, centre
    return width, height, angle, centre


def bbox_of(points):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def point_ring_distance(point, ring):
    """Shortest distance from a point to a ring's edge, in metres."""
    px, py = point
    best = float("inf")
    for i in range(len(ring) - 1):
        x1, y1 = ring[i]
        x2, y2 = ring[i + 1]
        dx, dy = x2 - x1, y2 - y1
        length = dx * dx + dy * dy
        if length <= 0:
            best = min(best, math.hypot(px - x1, py - y1))
            continue
        t = ((px - x1) * dx + (py - y1) * dy) / length
        t = 0.0 if t < 0 else (1.0 if t > 1 else t)
        best = min(best, math.hypot(px - x1 - t * dx, py - y1 - t * dy))
    return best


def point_in_ring(point, ring):
    """Standard crossing test. `ring` must be closed."""
    x, y = point
    inside = False
    for i in range(len(ring) - 1):
        x1, y1 = ring[i]
        x2, y2 = ring[i + 1]
        if (y1 > y) != (y2 > y):
            denom = y2 - y1
            if denom and x < (x2 - x1) * (y - y1) / denom + x1:
                inside = not inside
    return inside


# How far a path's two ends may sit apart and still count as closed, as a
# fraction of the path's own size. Drawn boundaries are routinely left with a
# small slot rather than joined: the gross-area outline in one real file is a
# 122-point polyline whose ends stop 58 mm short of each other, over a
# boundary 78 m long. Judging that gap against a fixed tolerance rejects it,
# and the floor then has no authored outline to use.
ENDS_MEET_FRAC = 0.01


def _ends_meet(path):
    """True when a path's two ends are close enough to call it closed."""
    x0, y0, x1, y1 = bbox_of(path)
    limit = max(CURVE_TOLERANCE, ENDS_MEET_FRAC * max(x1 - x0, y1 - y0))
    return math.dist(path[0], path[-1]) <= limit


def close_ring(points):
    """Return `points` with its first point repeated at the end."""
    if len(points) < 3:
        return None
    ring = [list(p) for p in points]
    if abs(ring[0][0] - ring[-1][0]) > 1e-9 or abs(ring[0][1] - ring[-1][1]) > 1e-9:
        ring.append(list(ring[0]))
    return ring if len(ring) >= 4 else None


def convex_hull(points):
    """Monotone-chain convex hull, returned as a closed ring."""
    pts = sorted(set((round(p[0], 6), round(p[1], 6)) for p in points))
    if len(pts) < 3:
        return None

    def half(seq):
        out = []
        for p in seq:
            while len(out) >= 2:
                ax, ay = out[-2]
                bx, by = out[-1]
                if (bx - ax) * (p[1] - ay) - (by - ay) * (p[0] - ax) > 0:
                    break
                out.pop()
            out.append(p)
        return out

    lower = half(pts)
    upper = half(reversed(pts))
    hull = lower[:-1] + upper[:-1]
    return close_ring(hull) if len(hull) >= 3 else None


# ─────────────────────────────────────────────────────────────────────────────
# Reading and flattening
# ─────────────────────────────────────────────────────────────────────────────

# Both kept here, re-exported from `dxf_reader`, which now owns the actual
# parsing (and the cache that lets it skip a second `ezdxf.read` when
# `dxf_bridge`'s raw import already parsed this exact drop) -- so every
# existing caller of `fs_clean.read_document`/`fs_clean.check_not_dwg` keeps
# working unchanged.
check_not_dwg = dxf_reader.check_not_dwg
read_document = dxf_reader.read_document


# A spline whose control points sit this close together, relative to the
# tolerance, is already described more finely than the tolerance asks for, so
# its control polyline is the answer. The factor of 3 is the safe bound: a
# cubic segment stays inside the convex hull of its four control points,
# whose diameter is at most three times the spacing.
CONTROL_POLYLINE_FACTOR = 3.0


def _spline_control_polyline(entity, tolerance):
    """Return a spline's control polyline when it is accurate enough.

    Some drawings define furniture curves absurdly finely: one real file
    holds 6000 splines whose shapes are 26 mm across and which carry 96
    control points each, a resolution of a quarter of a millimetre.
    Flattening those properly costs 17 seconds and every point past the
    first few is discarded by the raster, whose cells are 29 mm wide.
    """
    try:
        points = [(p[0], p[1]) for p in entity.control_points]
        if len(points) < 3:
            points = [(p[0], p[1]) for p in entity.fit_points]
    except Exception:
        return None
    if len(points) < 3:
        return None

    limit = tolerance / CONTROL_POLYLINE_FACTOR
    for start, end in zip(points, points[1:]):
        if math.dist(start, end) > limit:
            return None
    return [(points, bool(getattr(entity, "closed", False)))]


def _in_world_plane(entity):
    """True when an entity's own coordinates are already world coordinates.

    A polyline, arc or circle stores its points in its object coordinate
    system, which the extrusion vector defines. Reading them directly is
    only safe while that system is the world plane. A block placed mirrored
    arrives with extrusion (0, 0, -1), and reading its points as world
    coordinates flips the sign of x: the shape lands mirrored about the
    drawing origin, hundreds of metres from where it belongs, and the plan
    comes out twice as wide with a ghost copy of itself.
    """
    try:
        extrusion = entity.dxf.extrusion
    except Exception:
        return True
    return (abs(extrusion[0]) < 1e-9 and abs(extrusion[1]) < 1e-9
            and extrusion[2] > 0)


def _arc_points(entity, tolerance):
    """Flatten an ARC or CIRCLE directly, or None if it cannot be read.

    The step comes from the tolerance the caller asked for: the sagitta of a
    chord subtending an angle t on radius r is r(1 - cos(t/2)), so holding
    that at or below the tolerance gives the same accuracy as ezdxf's own
    flattening at a fraction of the cost. Door swings and tessellated
    circles are a large share of any drawing.
    """
    try:
        centre = entity.dxf.center
        radius = float(entity.dxf.radius)
        whole = entity.dxftype() == "CIRCLE"
        if whole:
            lo, hi = 0.0, 360.0
        else:
            lo, hi = float(entity.dxf.start_angle), float(entity.dxf.end_angle)
            if hi < lo:
                hi += 360.0     # a wrapped arc: 350 to 10 sweeps 20
    except Exception:
        return None
    if radius <= 0:
        return None
    # An arc whose start and end angles are equal draws nothing, which is
    # what AutoCAD and ezdxf both do with it. A spline-to-arc conversion
    # leaves these behind with absurd radii -- one real file holds 14 with
    # radii of thousands of kilometres -- so sweeping a full turn instead
    # scatters points millions of metres from the plan.
    span = hi - lo
    if span <= 0:
        return []

    ratio = min(1.0, max(0.0, tolerance / radius))
    step = math.degrees(2.0 * math.acos(1.0 - ratio)) if ratio > 0 else 1.0
    steps = max(8, int(math.ceil(span / max(step, 0.05))))
    steps = min(steps, 720)
    points = []
    for index in range(steps + 1):
        angle = math.radians(lo + (hi - lo) * index / steps)
        points.append((centre[0] + radius * math.cos(angle),
                       centre[1] + radius * math.sin(angle)))
    return [(points, whole)]


def entity_paths(entity, tolerance):
    """Return an entity's geometry as a list of point paths, plus closedness.

    Uses ezdxf's path model, which already understands bulged polylines,
    splines, ellipses and arcs, so every curve arrives flattened the same
    way. Falls back to reading vertices directly when the path model cannot
    handle an entity.
    """
    kind = entity.dxftype()

    if kind == "HATCH":
        # `ezdxf.path.from_hatch` rather than the boundary vertices, for two
        # reasons. A boundary can be an EdgePath of arcs, ellipses and
        # splines, which has no vertices to read at all. And `vertices` on a
        # PolylinePath is a list, not a method: the obvious `.vertices()`
        # raises TypeError, and with the call wrapped in `except Exception`
        # that silently returned no geometry for every hatch in every
        # drawing. A wall drawn as poché contributed nothing to the wall
        # mass, and nothing said so.
        paths = []
        if from_hatch is not None:
            try:
                for path in from_hatch(entity):
                    pts = [(p.x, p.y) for p in path.flattening(tolerance)]
                    if len(pts) >= 3:
                        paths.append((pts, True))
            except Exception:
                paths = []
        if not paths:
            try:
                for boundary in entity.paths:
                    raw = getattr(boundary, "vertices", None)
                    pts = [(v[0], v[1]) for v in raw] if raw else []
                    if len(pts) >= 3:
                        paths.append((pts, True))
            except Exception:
                pass
        if paths:
            return paths

    # Fast paths for the two kinds that carry no curve at all. Building a
    # path object and flattening it costs about half a millisecond, which is
    # nothing until a drawing arrives with its blocks exploded and hands us
    # forty thousand straight segments. These two cases are most of any
    # file, and reading their vertices is exact rather than approximate.
    if kind == "LINE":
        start, end = entity.dxf.start, entity.dxf.end
        return [([(start[0], start[1]), (end[0], end[1])], False)]

    if kind == "LWPOLYLINE" and _in_world_plane(entity):
        try:
            raw = entity.get_points("xyb")
            if not any(point[2] for point in raw):
                pts = [(point[0], point[1]) for point in raw]
                if len(pts) >= 2:
                    return [(pts, bool(entity.closed))]
        except Exception:
            pass

    if kind == "SPLINE":
        dense = _spline_control_polyline(entity, tolerance)
        if dense is not None:
            return dense

    if kind in ("ARC", "CIRCLE") and _in_world_plane(entity):
        arc = _arc_points(entity, tolerance)
        if arc is not None:
            return arc

    if make_path is not None and kind in (
            "LWPOLYLINE", "POLYLINE", "LINE", "ARC", "CIRCLE",
            "ELLIPSE", "SPLINE", "HELIX"):
        try:
            path = make_path(entity)
            pts = [(p.x, p.y) for p in path.flattening(tolerance)]
            if len(pts) >= 2:
                closed = (abs(pts[0][0] - pts[-1][0]) < 1e-9
                          and abs(pts[0][1] - pts[-1][1]) < 1e-9)
                if not closed:
                    closed = bool(getattr(entity, "closed", False)) or \
                        kind in ("CIRCLE", "ELLIPSE")
                return [(pts, closed)]
        except Exception:
            pass

    # Direct fallbacks.
    try:
        if kind == "LWPOLYLINE":
            pts = [(p[0], p[1]) for p in entity.get_points()]
            return [(pts, bool(entity.closed))] if len(pts) >= 2 else []
        if kind == "POLYLINE":
            pts = [(v.dxf.location[0], v.dxf.location[1]) for v in entity.vertices]
            return [(pts, bool(entity.is_closed))] if len(pts) >= 2 else []
        if kind == "LINE":
            s, e = entity.dxf.start, entity.dxf.end
            return [([(s[0], s[1]), (e[0], e[1])], False)]
        if kind in ("CIRCLE", "ARC"):
            c = entity.dxf.center
            r = entity.dxf.radius
            if kind == "CIRCLE":
                lo, hi = 0.0, 360.0
            else:
                lo, hi = entity.dxf.start_angle, entity.dxf.end_angle
                if hi < lo:
                    hi += 360.0
                if hi - lo <= 0:
                    return []           # degenerate, as above
            steps = max(8, int(abs(hi - lo) / 6))
            pts = []
            for i in range(steps + 1):
                ang = math.radians(lo + (hi - lo) * i / steps)
                pts.append((c[0] + r * math.cos(ang), c[1] + r * math.sin(ang)))
            return [(pts, kind == "CIRCLE")]
    except Exception:
        return []
    return []


class Item:
    """One flattened piece of the drawing, in metres.

    A top-level primitive becomes one item. A placed block becomes one item
    holding all of that block's geometry, so a chair stays one chair instead
    of becoming forty loose line fragments.
    """

    __slots__ = ("layer", "block", "etype", "paths", "text", "insert",
                 "element_type", "cls", "sub_class", "confidence",
                 "bbox", "center", "width", "height", "_oriented")

    def __init__(self, layer, block, etype, paths, text=None, insert=None):
        self.layer = layer or ""
        self.block = block or ""
        self.etype = etype
        self.paths = paths              # list of (points, closed)
        self.text = text
        self.insert = insert
        self.element_type = None
        self.cls = None
        self.sub_class = None
        self.confidence = "unknown"
        self.measure()

    def measure(self):
        self._oriented = None
        pts = [p for path, _ in self.paths for p in path]
        if not pts:
            self.bbox = self.center = None
            self.width = self.height = 0.0
            return
        x0, y0, x1, y1 = bbox_of(pts)
        self.bbox = (x0, y0, x1, y1)
        self.center = [(x0 + x1) / 2.0, (y0 + y1) / 2.0]
        self.width = x1 - x0
        self.height = y1 - y0

    def size(self):
        """(long, short) for a size rule to judge, in metres.

        The item's own extent, not its axis-aligned box. A desk turned 31
        degrees has a box 1.79 by 1.51 m where the desk is 1.6 by 0.8 m, and
        every rule that knows what a desk measures then misses it.

        Computed once and kept, because the size rules ask for it per item and
        rotating calipers is not free on a drawing with a hundred thousand of
        them. Falls back to the axis-aligned box for anything with too few
        points to have a hull, which is the right answer for those anyway.
        """
        if self._oriented is None:
            long_side, short_side = (max(self.width, self.height),
                                     min(self.width, self.height))
            fitted = min_area_rect(self.all_points())
            if fitted:
                long_side, short_side = fitted[0], fitted[1]
            self._oriented = (long_side, short_side)
        return self._oriented

    def all_points(self):
        return [p for path, _ in self.paths for p in path]

    def closed_rings(self):
        out = []
        for path, closed in self.paths:
            if closed or (len(path) >= 4 and _ends_meet(path)):
                ring = close_ring(path)
                if ring:
                    out.append(ring)
        return out

    def outline(self):
        """Best single closed outline for this item.

        Prefers a real closed ring that accounts for most of the footprint,
        because that is the drawn shape. Falls back to the convex hull, which
        is the right answer for anything drawn as loose arcs and lines.
        """
        rings = self.closed_rings()
        if rings:
            best = max(rings, key=ring_area)
            footprint = max(self.width * self.height, 1e-9)
            if ring_area(best) >= 0.5 * footprint:
                return best
        hull = convex_hull(self.all_points())
        if hull:
            return hull
        return None


def flatten(doc, scale, tolerance):
    """Flatten the modelspace into `Item`s, converted to metres.

    Nested blocks are resolved by ezdxf, which applies each INSERT's own
    transform. That is where a block's authored unit conversion is handled.
    """
    items = []
    texts = []
    msp = doc.modelspace()

    def collect(entity, depth, layer, block, out):
        kind = entity.dxftype()
        if kind == "INSERT":
            if depth >= MAX_BLOCK_DEPTH:
                return
            try:
                children = list(entity.virtual_entities())
            except Exception:
                return
            for child in children:
                collect(child, depth + 1, layer, block, out)
            return
        if kind in ("TEXT", "MTEXT", "ATTRIB"):
            return
        for pts, closed in entity_paths(entity, tolerance / max(scale, 1e-12)):
            scaled = [(p[0] * scale, p[1] * scale) for p in pts]
            if len(scaled) >= 2 or closed:
                out.append((scaled, closed))

    for entity in msp:
        kind = entity.dxftype()
        layer = entity.dxf.layer if entity.dxf.hasattr("layer") else ""

        if kind in ("TEXT", "MTEXT"):
            try:
                content = entity.dxf.text if kind == "TEXT" else entity.text
                insert = entity.dxf.insert
                if kind == "MTEXT":
                    insert = entity.dxf.insert
                texts.append({
                    "layer": layer,
                    "text": _clean_text(content),
                    "point": [insert[0] * scale, insert[1] * scale],
                })
            except Exception:
                pass
            continue

        if kind in ("VIEWPORT", "ATTDEF", "DIMENSION", "LEADER", "MLEADER"):
            continue

        if kind == "INSERT":
            block_name = entity.dxf.name
            paths = []
            collect(entity, 0, layer, block_name, paths)
            if not paths:
                continue
            insert = None
            try:
                insert = [entity.dxf.insert[0] * scale, entity.dxf.insert[1] * scale]
            except Exception:
                pass
            items.append(Item(layer, block_name, "INSERT", paths, insert=insert))
            continue

        paths = []
        for pts, closed in entity_paths(entity, tolerance / max(scale, 1e-12)):
            scaled = [(p[0] * scale, p[1] * scale) for p in pts]
            paths.append((scaled, closed))
        if paths:
            items.append(Item(layer, "", kind, paths))

    return items, texts


_MTEXT_CODES = ("\\P", "\\p", "\\~")


def _clean_text(value):
    """Strip MTEXT formatting codes down to readable text."""
    if not value:
        return ""
    out = str(value)
    for code in _MTEXT_CODES:
        out = out.replace(code, " ")
    # Drop inline formatting runs such as \fArial|b0; and {\H0.8x; ... }
    result = []
    i = 0
    while i < len(out):
        ch = out[i]
        if ch == "\\" and i + 1 < len(out):
            j = i + 1
            while j < len(out) and out[j] not in ";" and (out[j].isalnum()
                                                          or out[j] in ".|-+*"):
                j += 1
            i = j + 1 if j < len(out) and out[j] == ";" else i + 2
            continue
        if ch in "{}":
            i += 1
            continue
        result.append(ch)
        i += 1
    return " ".join("".join(result).split())


# ─────────────────────────────────────────────────────────────────────────────
# Routing
# ─────────────────────────────────────────────────────────────────────────────

# Everything that contributes ink to the wall mass. Glazing is included
# because a window sits in a wall: its frame line is part of the envelope,
# even though the window is also exported as its own element.
WALL_MASS_CLASSES = {"Wall", "Partition", "Column", "Window", "Stair",
                     "Railing", "Other"}
# Structure classes that also become their own features rather than only
# feeding the wall mass.
WINDOW_CLASSES = {"Window"}

# Every value a piece of furniture's `confidence` may take, in decreasing
# order of how much the drawing itself told us. The UI reports the share that
# is better than `layer` or `unknown`, so a new value added to the pipeline
# and not listed here would silently change that figure.
#
# `geometry` is the recovery pipeline's value. It means the piece was
# identified from shape alone, on a drawing that named nothing: no block
# name, no useful layer, only how it looks and what it repeats with.
CONFIDENCE_VALUES = ("name", "size+space", "size", "space", "geometry",
                     "layer", "unknown")
# The values that mean the drawing said what something was, rather than the
# pipeline working it out. Reported as the complement, so this list is the
# only place the distinction is written down.
CONFIDENCE_VAGUE = ("layer", "unknown")


def classify_items(items):
    """Attach a taxonomy classification to every item."""
    for item in items:
        intent = tax.classify_layer_intent(item.layer)
        if intent == "annotation":
            item.element_type, item.cls = "Annotation", None
            item.confidence = "layer"
            continue
        if intent == "space":
            item.element_type, item.cls = "SpaceOverlay", None
            item.confidence = "layer"
            continue

        if item.etype == "INSERT":
            et, cls, sub, conf = tax.classify_block(
                item.block, item.layer, *item.size())
        else:
            # Loose geometry carries no name, so the layer is the only
            # evidence. A named role is worth having: a drawing that keeps
            # its doors on A-DOOR and its glazing on A-GLAZ has said which
            # is which, and reading only the discipline throws that away.
            et, cls, sub, conf = None, None, None, "unknown"
            role = tax.classify_layer_role(item.layer)
            if role:
                et, cls, sub = role
                conf = "layer"
            elif intent == "structure":
                et, cls, conf = "Structure", "Other", "layer"
            elif intent == "furniture":
                et, cls, conf = "Furniture", "Other", "layer"

        item.element_type, item.cls, item.sub_class, item.confidence = \
            et, cls, sub, conf
    return items


# Classes whose fragments must stay separate when grouping. A wall's pieces
# touch each other from one end of the building to the other, so grouping
# them yields one object the size of the floor: no use to anybody, and it
# destroys the thickness measurement that reads each band's short side.
# Keyed on (element_type, class), because a furniture fragment the layer
# could not name is also "Other" and it does need grouping.
NO_GROUP_CLASSES = {("Structure", "Wall"), ("Structure", "Other")}


def group_loose_geometry(items, gap=LOOSE_GROUP_GAP,
                         max_span=LOOSE_GROUP_MAX_SPAN):
    """Merge touching loose fragments into one item per object.

    Returns (items, report).

    A DXF exported with its blocks exploded has no names and no objects: one
    chair arrives as forty unrelated arcs, one door as two lines and a
    swing. Every count is then a count of fragments and every size test
    reads a 10 mm sliver, so nothing can be classified and the furniture
    total runs into the tens of thousands.

    Fragments of one object touch. Separate objects normally do not. So
    proximity within a layer recovers the objects, which is the best that
    can be done once the names are gone.
    """
    report = {"grouped": 0, "into": 0, "oversized": 0}
    groupable = defaultdict(list)
    passthrough = []
    for index, item in enumerate(items):
        if (item.etype == "INSERT" or item.bbox is None
                or item.element_type not in ("Structure", "Furniture", "Decor")
                or (item.element_type, item.cls) in NO_GROUP_CLASSES):
            passthrough.append(item)
        else:
            groupable[(item.element_type, item.cls, item.sub_class,
                       item.layer)].append(item)

    merged = []
    for key, members in groupable.items():
        for group in _proximity_groups(members, gap):
            if len(group) == 1:
                merged.append(group[0])
                continue
            x0 = min(m.bbox[0] for m in group)
            y0 = min(m.bbox[1] for m in group)
            x1 = max(m.bbox[2] for m in group)
            y1 = max(m.bbox[3] for m in group)
            if max(x1 - x0, y1 - y0) > max_span:
                report["oversized"] += 1
                merged.extend(group)
                continue
            first = group[0]
            whole = Item(first.layer, first.block, first.etype,
                         [p for m in group for p in m.paths])
            whole.element_type = first.element_type
            whole.cls, whole.sub_class = first.cls, first.sub_class
            whole.confidence = first.confidence
            merged.append(whole)
            report["grouped"] += len(group)
            report["into"] += 1

    return passthrough + merged, report


def _bbox_proximity_groups(boxes, gap):
    """Group the indices of `boxes` that come within `gap` of each other.

    Buckets the boxes on a coarse grid so each one is compared only with its
    neighbours, then unions the pairs that are close enough. Union-find
    rather than a flood fill, because the chains are long and shallow.

    Takes plain boxes rather than objects carrying one, so the same
    clustering serves both fragments of a drawing (`_proximity_groups`, which
    reads `Item.bbox`) and finished furniture pieces (`desk_groups`, which
    reads a ring). Two copies of a union-find would be two things to keep in
    step, and this file already carries the lesson from having had two room
    tracers.
    """
    count = len(boxes)
    parent = list(range(count))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]      # halve the path as we go
            a = parent[a]
        return a

    cell = max(gap * 2.0, 0.05)
    buckets = defaultdict(list)
    for index, (x0, y0, x1, y1) in enumerate(boxes):
        for gx in range(int(math.floor((x0 - gap) / cell)),
                        int(math.floor((x1 + gap) / cell)) + 1):
            for gy in range(int(math.floor((y0 - gap) / cell)),
                            int(math.floor((y1 + gap) / cell)) + 1):
                buckets[(gx, gy)].append(index)

    for indices in buckets.values():
        for position, a in enumerate(indices):
            ax0, ay0, ax1, ay1 = boxes[a]
            for b in indices[position + 1:]:
                bx0, by0, bx1, by1 = boxes[b]
                if (ax0 - gap <= bx1 and bx0 - gap <= ax1
                        and ay0 - gap <= by1 and by0 - gap <= ay1):
                    root_a, root_b = find(a), find(b)
                    if root_a != root_b:
                        parent[root_b] = root_a

    groups = defaultdict(list)
    for index in range(count):
        groups[find(index)].append(index)
    return list(groups.values())


def _proximity_groups(items, gap):
    """Group items whose bounding boxes come within `gap` of each other."""
    return [[items[index] for index in group]
            for group in _bbox_proximity_groups([i.bbox for i in items], gap)]


def classify_grouped(items):
    """Name the objects that grouping has just recovered.

    A group has a footprint where its fragments had none, so a size rule can
    speak for the first time. Only fill in what is still blank: a layer that
    named its doors has already said more than a size ever can.
    """
    for item in items:
        if item.cls not in (None, "Other") or item.element_type not in (
                "Furniture", "Decor"):
            continue
        # The item's own extent, not its axis-aligned box. A rotated
        # drawing measures every desk too deep otherwise.
        guess = tax.classify_by_size(*item.size())
        if guess:
            item.element_type, item.cls, item.sub_class = guess
            item.confidence = "size"
    return items


def drop_off_plan(items, texts):
    """Remove content that does not sit on the floor plate.

    Returns (kept_items, kept_texts, report). A drawing routinely carries a
    mirrored backup copy or a stray the conversion misplaced. Including it
    inflates the plan's bounding box, so the view zooms out to frame empty
    space, and it invents rooms nobody can stand in.
    """
    # One extent per item, not a cloud of vertices. A wall drawn as a long
    # rectangle has only its four corners, and clustering those sees a gap
    # the width of the wall's own length.
    boxes = [item.bbox for item in items if item.bbox]
    cluster = fs_units.floor_cluster(boxes)
    if not cluster:
        return items, texts, {"dropped": 0, "reason": "no cluster"}

    x0, y0, x1, y1 = cluster["bbox"]
    margin = OFF_PLAN_MARGIN * max(x1 - x0, y1 - y0)
    x0, y0, x1, y1 = x0 - margin, y0 - margin, x1 + margin, y1 + margin

    kept, dropped = [], []
    empty = 0
    for item in items:
        if item.center is None:
            # No geometry to place. Counted rather than silently lost, so the
            # report's totals still add up to what came in.
            empty += 1
            continue
        cx, cy = item.center
        if x0 <= cx <= x1 and y0 <= cy <= y1:
            kept.append(item)
        else:
            dropped.append(item)

    kept_texts = [t for t in texts
                  if x0 <= t["point"][0] <= x1 and y0 <= t["point"][1] <= y1]

    by_layer = Counter(item.layer for item in dropped)
    return kept, kept_texts, {
        "dropped": len(dropped),
        "empty": empty,
        "dropped_by_layer": dict(by_layer),
        "floor_bbox": [x0 + margin, y0 + margin, x1 - margin, y1 - margin],
        "covers": round(cluster["covers"], 4),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Wall mass
# ─────────────────────────────────────────────────────────────────────────────

def estimate_wall_thickness(items, fallback=0.1):
    """Measure the repeated wall thickness, in metres, from wall bands.

    Used when the unit report carries no measurement, which happens when a
    caller supplies a known scale and skips measuring the document.

    This deliberately mirrors `fs_units`: filter candidate bands against the
    plan's own size, then take the mode of log-spaced buckets. A plain
    rounded mode over every closed band on a structure layer lands on the
    door-stop and mullion slivers rather than the walls, and reports 50 mm
    where the walls are 100 mm. That halves the raster resolution and the
    thickening radius, so the wall mass comes out too thin to see while
    every count, area and element total still looks correct.
    """
    boxes = [item.bbox for item in items if item.bbox]
    if not boxes:
        return fallback
    extent = max(max(b[2] - b[0] for b in boxes),
                 max(b[3] - b[1] for b in boxes))
    if extent <= 0:
        return fallback

    shorts, on_wall_layers = [], []
    for item in items:
        if item.element_type != "Structure":
            continue
        on_wall = bool(fs_units.WALL_LAYER.search(item.layer))
        for ring, closed in item.paths:
            if not closed and not on_wall:
                continue
            if len(ring) < 2:
                continue
            x0, y0, x1, y1 = bbox_of(ring)
            short, long_ = min(x1 - x0, y1 - y0), max(x1 - x0, y1 - y0)
            if short <= 0:
                continue
            if (long_ / short >= fs_units.BAND_MIN_ASPECT
                    and long_ >= fs_units.BAND_MIN_LONG_FRAC * extent
                    and short <= fs_units.BAND_MAX_SHORT_FRAC * extent):
                (on_wall_layers if on_wall else shorts).append(short)

    # Same preference as `fs_units`: a wall layer's own bands beat the
    # drawing-wide mode, which glazing lines and desk screens outnumber.
    if len(on_wall_layers) >= fs_units.WALL_BAND_MIN_SAMPLES:
        measured = fs_units._mode_of(on_wall_layers)
    else:
        measured = fs_units._mode_of(shorts + on_wall_layers)
    if not measured or not (fs_units.WALL_MIN <= measured <= fs_units.WALL_MAX):
        return fallback
    return float(measured)


def take_floor_zone(items, bbox):
    """Pull the floor-outline polygon out of the space overlay, if there is one.

    Returns (rings, remaining_items). A zone polygon is the building's own
    footprint. It is the most faithful floor outline available and it must
    not be left among the rooms.
    """
    plan_area = max((bbox[2] - bbox[0]) * (bbox[3] - bbox[1]), 1e-9)
    rings = []
    remaining = []
    for item in items:
        if item.element_type != "SpaceOverlay":
            remaining.append(item)
            continue
        zone_rings = [r for r in item.closed_rings()
                      if ring_area(r) >= FLOOR_ZONE_AREA_FRAC * plan_area]
        if zone_rings:
            rings.extend(zone_rings)
            continue
        remaining.append(item)
    return rings, remaining


def floor_footprint(items, zone_rings, grid, wall_thickness):
    """Return a mask of the whole floor slab, holes filled.

    The footprint is the union of two readings, because neither is complete
    on its own:

    * the zone polygon the drawing supplies, when it has one. This gives a
      clean outer boundary along a glazed facade, which no amount of
      geometry can infer.
    * a footprint derived by bridging everything the plan contains into one
      blob. This covers whatever the zone polygon leaves out.

    Taking the union matters. A drawing's own area boundary is often a *net*
    area: it stops at the core and excludes the toilets, the risers and the
    plant. Used alone it clips a third of the wall mass away, and the plan
    comes out with whole blocks of rooms missing their walls.
    """
    derived = _derived_footprint(items, grid)

    if not zone_rings:
        return derived

    zone = raster.Grid.like(grid)
    raster.rasterise(zone, zone_rings, closed_fill=True)
    zone_mask = raster.fill_holes(zone.mask)
    if derived is None:
        return zone_mask
    return zone_mask | derived


def _derived_footprint(items, grid):
    """Footprint inferred from the drawing's own content, or None."""
    footprint = raster.Grid.like(grid)
    paths = []
    for item in items:
        if item.element_type in ("Annotation",):
            continue
        for path, _ in item.paths:
            if len(path) >= 2:
                paths.append(path)
    if not paths:
        return None

    raster.rasterise(footprint, paths)
    bridge = max(2, int(round(FOOTPRINT_BRIDGE_M / footprint.cell)))
    # Closing bridges the gaps between mullions without changing the outer
    # extent, since a close is a dilation followed by a matching erosion.
    # Nothing may be eroded afterwards: that would pull the whole outline
    # inward by the bridge distance, on every side.
    blob = raster.close_mask(footprint.mask, bridge)
    blob = raster.largest_component(blob)
    return raster.fill_holes(blob)


def build_walls(items, bbox, wall_thickness, zone_rings=None):
    """Reconstruct the wall mass and return (polygons, grid, mask).

    Feeds every structural mark into one raster, closes it so each wall's
    faces fuse into a single band, then traces the outline. This is what
    makes the output independent of how the source drawing chose to draw a
    wall.
    """
    cell = min(CELL_MAX, max(CELL_MIN, wall_thickness * CELL_PER_WALL))
    # A margin wide enough for the largest structuring element used on this
    # grid, so the perimeter band and the footprint close both have room to
    # work against the plan's outer edge.
    pad = int(math.ceil(FOOTPRINT_BRIDGE_M / cell)) + 4
    grid = raster.Grid(bbox, cell, pad_cells=pad)

    # A closed ring only gets filled solid when it is band-shaped. A wall
    # band and a hatch are solid, but a closed polyline that outlines a whole
    # room is not: filling that would black out the room.
    band_limit = wall_thickness * 2.5

    outline_paths = []
    solid_paths = []
    for item in items:
        if item.element_type != "Structure":
            continue
        if item.cls not in WALL_MASS_CLASSES:
            continue
        for path, closed in item.paths:
            if len(path) < 2:
                continue
            if closed and len(path) >= 4:
                x0, y0, x1, y1 = bbox_of(path)
                if min(x1 - x0, y1 - y0) <= band_limit:
                    solid_paths.append(path)
                    continue
            outline_paths.append(path)

    footprint = floor_footprint(items, zone_rings, grid, wall_thickness)

    if not outline_paths and not solid_paths:
        return [], grid, grid.mask, footprint

    raster.rasterise(grid, outline_paths)
    if solid_paths:
        raster.rasterise(grid, solid_paths, closed_fill=True)

    # Close by rather more than half the wall, so the two faces of a wall
    # meet in the middle and small jamb breaks seal, then despeckle.
    close_radius = max(2, int(math.ceil(wall_thickness * 0.75 / grid.cell)))
    mask = raster.close_mask(grid.mask, close_radius)
    # Thicken before despeckling, never after. A wall drawn as a single line
    # is one cell wide, and an opening of radius 1 erodes a one-cell feature
    # to nothing. Doing it the other way round deletes every single-line
    # interior wall in the drawing, so rooms either side of one merge into
    # a single space and the plan quietly loses its partitions.
    mask = raster.thicken_thin(
        mask, max(1, int(round(wall_thickness * 0.5 / grid.cell))))
    mask = raster.open_mask(mask, 1)

    # Add the building envelope. A curtain-wall facade is drawn as separate
    # mullions with glazing between them, so tracing the drawing alone leaves
    # the floor with no outer wall at all. The envelope comes from the
    # footprint instead, which is where the boundary actually is.
    if footprint is not None and footprint.any():
        band = max(1, int(round(wall_thickness * PERIMETER_FACTOR / grid.cell)))
        mask = mask | raster.boundary_band(footprint, band)
        # Nothing outside the building can be part of its walls.
        mask = mask & footprint

    min_cells = max(4, int(MIN_WALL_FRAGMENT_M2 / (grid.cell ** 2)))
    polygons = raster.trace_polygons(
        mask, grid, simplify=grid.cell * 1.2, min_area_cells=min_cells)
    return polygons, grid, mask, footprint


# ─────────────────────────────────────────────────────────────────────────────
# Spaces
# ─────────────────────────────────────────────────────────────────────────────

def spaces_from_overlay(items, texts):
    """Build spaces from a space-planning overlay, when the file has one.

    An overlay such as Serraview's is authoritative: a human drew one polygon
    per space and labelled it. Nothing derived from wall geometry beats that,
    so it is used whenever it is present.
    """
    overlay = [i for i in items if i.element_type == "SpaceOverlay"]
    if not overlay:
        return [], {}

    labels = [t for t in texts
              if tax.classify_layer_intent(t["layer"]) == "annotation"
              and t["text"]]

    spaces = []
    for item in overlay:
        for ring in item.closed_rings():
            area = ring_area(ring)
            if area < MIN_SPACE_AREA:
                continue
            spaces.append({
                "rings": [ring],
                "area": area,
                "center": ring_centroid(ring),
                "labels": [],
                "source": "overlay",
                "layer": item.layer,
            })

    matched = match_labels_to_spaces(spaces, labels)
    report = {
        "overlay_polygons": len(spaces),
        "labels": len(labels),
        "labels_matched": matched,
    }
    return spaces, report


def match_labels_to_spaces(spaces, labels):
    """Assign each label to one space. Returns how many were placed.

    An overlay carries one label of each kind per polygon, so the job is a
    one-to-one assignment, not an independent nearest-neighbour lookup per
    label. Two things break the naive version:

    Containment alone is not decisive, because overlay polygons overlap. A
    polygon can contain its own label and its neighbour's, which leaves the
    neighbour bare while one space collects two of everything.

    Nearest alone is not decisive either, because a label's insert point is
    a text anchor. On a desk-sized polygon it commonly lands just outside
    the edge it names.

    So every label-to-space pairing is costed, containment first and
    distance second, and the cheapest pairings are taken in order while at
    most one label of each kind is allowed per space. Cheap, and it holds
    the one-per-space structure the overlay really has.
    """
    if not spaces or not labels:
        return 0

    boxes = [bbox_of(space["rings"][0]) for space in spaces]

    pairs = []
    for label_index, label in enumerate(labels):
        px, py = label["point"]
        for space_index, space in enumerate(spaces):
            x0, y0, x1, y1 = boxes[space_index]
            # Skip anything the label cannot possibly belong to.
            if not (x0 - LABEL_SNAP_M <= px <= x1 + LABEL_SNAP_M
                    and y0 - LABEL_SNAP_M <= py <= y1 + LABEL_SNAP_M):
                continue
            ring = space["rings"][0]
            if point_in_ring((px, py), ring):
                # Contained beats near. Among containers the smallest
                # polygon wins, which is what puts a desk label on the desk
                # rather than on the zone drawn around it.
                cost = (0, space["area"])
            else:
                distance = point_ring_distance((px, py), ring)
                if distance >= LABEL_SNAP_M:
                    continue
                cost = (1, distance)
            pairs.append((cost, label_index, space_index))

    pairs.sort()

    # Build each label's candidate spaces, cheapest first.
    candidates = defaultdict(list)
    for _, label_index, space_index in pairs:
        candidates[label_index].append(space_index)

    # Labels of different kinds are independent: a space takes one type
    # label and one name label, so each kind is matched on its own.
    by_layer = defaultdict(list)
    for label_index, label in enumerate(labels):
        by_layer[label["layer"]].append(label_index)

    matched = 0
    assigned = {}
    for layer, label_indices in by_layer.items():
        owner = _max_matching(label_indices, candidates)
        for space_index, label_index in owner.items():
            assigned[label_index] = space_index
            matched += 1

    for label_index, space_index in assigned.items():
        spaces[space_index]["labels"].append(labels[label_index])

    # A label with no candidate at all keeps its text out of the model, so
    # place it on its cheapest candidate even if that space is taken.
    for label_index in range(len(labels)):
        if label_index in assigned:
            continue
        options = candidates.get(label_index)
        if options:
            spaces[options[0]]["labels"].append(labels[label_index])
            matched += 1

    return matched


def _max_matching(label_indices, candidates):
    """Maximum bipartite matching between labels and spaces.

    Returns {space_index: label_index}.

    Taking the cheapest pairing first and moving on does not work here.
    Overlay polygons overlap, and a desk label is anchored about half a desk
    away from its own polygon, so the closest polygon to a label is often
    its neighbour's. Greedy lets one label claim a polygon that another
    label needs, and the mistake cascades down a row of desks: some
    polygons end up with two labels and their neighbours with none.

    Augmenting paths fix that. When a label's candidates are all taken, the
    holder is asked to move to one of its own other candidates, recursively.
    Since each label's candidate list is ordered by cost, the cheap pairings
    are still preferred, but no label is left out when a valid arrangement
    for it exists.
    """
    owner = {}

    def try_place(label_index, visited, depth):
        # Bounded, because the search recurses once per displaced label. A
        # plan with thousands of spaces would otherwise exhaust the stack,
        # and Pyodide's is shallower than CPython's. A capped search still
        # resolves the chains that occur in practice, which are short.
        if depth > MAX_AUGMENT_DEPTH:
            return False
        for space_index in candidates.get(label_index, ()):
            if space_index in visited:
                continue
            visited.add(space_index)
            held_by = owner.get(space_index)
            if held_by is None or try_place(held_by, visited, depth + 1):
                owner[space_index] = label_index
                return True
        return False

    for label_index in label_indices:
        try_place(label_index, set(), 0)
    return owner


def _nearest_true_cell(mask, row, col, max_radius):
    """The True cell in `mask` nearest to (row, col), searching an expanding
    box out to `max_radius` cells. None if nothing is found that close.

    A label's own insert point routinely lands outside the room it names --
    up to several tens of centimetres for a text anchor on a small polygon --
    so matching it to the room requires this slack rather than an exact hit.
    """
    rows, cols = mask.shape
    if 0 <= row < rows and 0 <= col < cols and mask[row, col]:
        return row, col
    for radius in range(1, max_radius + 1):
        r0, r1 = max(0, row - radius), min(rows, row + radius + 1)
        c0, c1 = max(0, col - radius), min(cols, col + radius + 1)
        if r1 <= r0 or c1 <= c0:
            continue
        sub = mask[r0:r1, c0:c1]
        if not sub.any():
            continue
        rr, cc = np.nonzero(sub)
        dist = (rr + r0 - row) ** 2 + (cc + c0 - col) ** 2
        i = int(np.argmin(dist))
        return int(rr[i] + r0), int(cc[i] + c0)
    return None


def _split_leaked_regions(labels, sizes, border, grid, texts):
    """Watershed every region over `MAX_ROOM_AREA` by the labels sitting in it.

    A region this size is what a floor's partitions not fully closing looks
    like: the empty space of every room whose wall failed to shut flows
    together into one blob, and `label_traced_spaces` already refuses to
    name it after every room it swallowed. But each of those rooms still has
    its own name label sitting inside it, at ordinary room spacing, and a
    label is closer to its own room than to its neighbour's in every real
    building -- the gap between them is one missing wall, not a missing
    floor. Splitting the blob by nearest label recovers the rooms the wall
    failed to.

    Returns new (labels, sizes): the oversized label is replaced by one new
    label per seed that claimed at least one cell, plus (if any cell in the
    blob reached no seed at all) the original label for what is left. A
    region with fewer than two usable seeds is left exactly as it was, so a
    genuinely single large room -- an atrium, a warehouse floor -- is never
    split just for being big.
    """
    usable = [t for t in texts if t.get("text")]
    if not usable:
        return labels, sizes

    max_search = max(1, int(round(1.2 / grid.cell)))
    next_label = max(sizes) + 1 if sizes else 1

    for label, cells in list(sizes.items()):
        if label in border or cells * grid.cell ** 2 <= MAX_ROOM_AREA:
            continue

        full_region = labels == label
        rows_present, cols_present = np.nonzero(full_region)
        r0, r1 = int(rows_present.min()), int(rows_present.max())
        c0, c1 = int(cols_present.min()), int(cols_present.max())
        margin = max_search
        x0, y0 = (float(v) for v in grid.to_world(c0 - margin, r0 - margin))
        x1, y1 = (float(v) for v in grid.to_world(c1 + margin, r1 + margin))

        # `nearest_seed_labels` grows every seed's frontier by a ring of the
        # whole array each round. A leaked region is a small fraction of a
        # real floor's raster -- millions of cells -- so cropping to this
        # region's own bounding box before flooding it is what keeps the
        # cost proportional to the leak, not to the floor.
        region = full_region[r0:r1 + 1, c0:c1 + 1]

        # Keyed by cell, not appended positionally: two texts snapping to the
        # same cell (near-duplicate labels, or two just close enough that
        # `_nearest_true_cell` resolves them to one spot) must count once,
        # not silently cost one of them its seed -- `nearest_seed_labels`
        # would otherwise let the second overwrite the first with no sign
        # anything was lost.
        seed_cells = {}
        for text in usable:
            px, py = text["point"]
            if not (x0 <= px <= x1 and y0 <= py <= y1):
                continue
            cc, rr = grid.to_cell(np.array([px]), np.array([py]))
            hit = _nearest_true_cell(region, int(rr[0]) - r0, int(cc[0]) - c0,
                                     max_search)
            if hit is not None:
                seed_cells.setdefault(hit, len(seed_cells) + 1)
        if len(seed_cells) < 2:
            continue
        seeds = [(r, c, local) for (r, c), local in seed_cells.items()]

        owner = raster.nearest_seed_labels(region, seeds)
        claimed = sorted(int(v) for v in np.unique(owner) if v > 0)
        if len(claimed) != len(seeds):
            # A seed with no cells to its own name (should not happen once
            # seeds are deduplicated by cell, but this is the guard against
            # ever silently reporting fewer rooms than labels went in) --
            # abandon rather than accept a split one room short.
            continue

        # `texts` is every TEXT/MTEXT in the drawing, not only room names --
        # a furniture tag or a note sitting in the leaked area seeds a split
        # exactly like a real room label would. A real building's room
        # labels are one per room, at room spacing; anything denser than
        # that is stray text, not more rooms, and produces pieces smaller
        # than a room can honestly be. Demanding every piece clear
        # `MIN_ROOM_AREA` is what tells the two apart -- measured on a real
        # leaked region dense with non-room text, this is what keeps 60
        # genuine room labels from becoming 400 phantom ones: it abandons
        # the split rather than accept a single undersized piece, leaving
        # the region exactly as it was so the existing leak warning still
        # fires, honestly, instead of reporting a wrong room count with
        # confidence.
        piece_areas = [int((owner == local).sum()) * grid.cell ** 2
                       for local in claimed]
        if min(piece_areas) < MIN_ROOM_AREA:
            continue

        # Written back through a view onto the cropped window, so this lands
        # at the right place in the full-size array without ever building a
        # full-size mask.
        labels_window = labels[r0:r1 + 1, c0:c1 + 1]
        for local in claimed:
            piece = owner == local
            labels_window[piece] = next_label
            sizes[next_label] = int(piece.sum())
            next_label += 1

        unclaimed = region & (owner == 0)
        if unclaimed.any():
            sizes[label] = int(unclaimed.sum())
        else:
            del sizes[label]

    return labels, sizes


def spaces_from_walls(mask, grid, wall_thickness, texts=None):
    """Trace enclosed rooms as the connected empty regions inside the walls.

    This replaces walking a graph of wall segments looking for cycles. A
    cycle walk needs a topologically perfect wall network and invents rooms
    wherever the network has a gap. Connected empty space needs neither.
    """
    empty = ~mask
    min_cells = max(4, int(MIN_ROOM_AREA / (grid.cell ** 2)))
    labels, sizes = raster.label_regions(empty, min_cells=min_cells)
    if not sizes:
        return []

    # The region touching the raster border is the outside world.
    border = set(labels[0, :].tolist()) | set(labels[-1, :].tolist()) \
        | set(labels[:, 0].tolist()) | set(labels[:, -1].tolist())
    border.discard(0)

    if texts:
        labels, sizes = _split_leaked_regions(labels, sizes, border, grid, texts)

    candidates = []
    for label, cells in sizes.items():
        if label in border:
            continue
        area = cells * grid.cell ** 2
        if area < MIN_ROOM_AREA:
            continue
        candidates.append((label, area))
    if not candidates:
        return []

    largest = max(area for _, area in candidates)
    spaces = []
    for label, area in candidates:
        if area > MAX_ROOM_AREA and area < largest:
            continue
        polygons = raster.trace_polygons(labels == label, grid,
                                         simplify=grid.cell * 1.5,
                                         min_area_cells=1)
        if not polygons:
            continue
        rings = polygons[0]
        # Grow the room back out by half a wall. The empty region stops at
        # the wall face, but a room is measured to the middle of its walls.
        spaces.append({
            "rings": rings,
            "area": ring_area(rings[0]) - sum(ring_area(r) for r in rings[1:]),
            "center": ring_centroid(rings[0]),
            "labels": [],
            "source": "traced",
            "layer": "",
        })
    return spaces


def residual_spaces(spaces, mask, grid, footprint, wall_thickness):
    """Trace the floor area that no space polygon already covers.

    A space overlay names the rooms and the desks but usually leaves the open
    plan and the circulation between them blank. Without this the plan has
    hundreds of square metres belonging to no space, and every chair standing
    in the open belongs to nothing.

    The residual area is whatever is inside the building, outside the walls,
    and outside every space already known.
    """
    if footprint is None or not footprint.any():
        return []

    covered = raster.Grid.like(grid)
    rings = [ring for space in spaces for ring in space["rings"]]
    if rings:
        raster.rasterise(covered, rings, closed_fill=True)

    free = footprint & ~mask & ~covered.mask
    # Clear the slivers left along every wall face and space edge.
    free = raster.open_mask(free, max(1, int(round(0.15 / grid.cell))))
    if not free.any():
        return []

    min_cells = max(4, int(MIN_OPEN_AREA / (grid.cell ** 2)))
    labels, sizes = raster.label_regions(free, min_cells=min_cells)

    out = []
    for label in sizes:
        polygons = raster.trace_polygons(labels == label, grid,
                                         simplify=grid.cell * 2.0,
                                         min_area_cells=min_cells)
        if not polygons:
            continue
        for polygon in polygons:
            area = ring_area(polygon[0]) - sum(ring_area(r) for r in polygon[1:])
            if area < MIN_OPEN_AREA:
                continue
            out.append({
                "rings": polygon,
                "area": area,
                "center": ring_centroid(polygon[0]),
                "labels": [],
                "source": "residual",
                "layer": "",
            })
    return out


def classify_residual(space):
    """Name a residual region from its own shape.

    A long thin region is circulation. A broad one is open workspace. This is
    the honest limit of what shape alone can say, so no finer guess is made.
    """
    x0, y0, x1, y1 = bbox_of(space["rings"][0])
    width, height = x1 - x0, y1 - y0
    long_side, short_side = max(width, height), max(min(width, height), 1e-9)
    # Compactness against the bounding box separates a corridor network,
    # which fills little of its box, from an open floor which fills most.
    fill = space["area"] / max(long_side * short_side, 1e-9)
    if long_side / short_side >= 3.0 or fill < 0.35:
        return "Circulate", "Corridor"
    return "Work", "Open Workspace"


def label_traced_spaces(spaces, texts):
    """Attach any room-name text that falls inside a traced room.

    Each label goes to the smallest room that contains it, and a room over
    `MAX_ROOM_AREA` takes none at all. Both rules exist for the same reason.
    Where a drawing's partitions do not close, the open area comes back as
    one region covering most of the floor, and it geometrically contains the
    label of every room that failed to close. Handing it all of them names
    that space after five hundred rooms at once, which is worse than leaving
    it unnamed: unnamed, its area still classifies it.
    """
    usable = [t for t in texts if t["text"]]
    candidates = sorted(
        (s for s in spaces
         if s["source"] == "traced" and s["area"] <= MAX_ROOM_AREA),
        key=lambda s: s["area"])
    # A bounding box first: point_in_ring is an edge test per point on the
    # ring, and a real floor can have hundreds of traced rooms and thousands
    # of text labels. Most pairs are nowhere near each other, so an O(1)
    # box check ahead of it is what keeps this from costing a full ring scan
    # for all of them.
    boxes = [bbox_of(space["rings"][0]) for space in candidates]
    for text in usable:
        px, py = text["point"]
        for space, (x0, y0, x1, y1) in zip(candidates, boxes):
            if not (x0 <= px <= x1 and y0 <= py <= y1):
                continue
            if point_in_ring(text["point"], space["rings"][0]):
                space["labels"].append(text)
                break
    return spaces


# ─────────────────────────────────────────────────────────────────────────────
# Openings
# ─────────────────────────────────────────────────────────────────────────────

def _wall_axis_near(mask, grid, bbox, reach):
    """Find which way the wall runs near `bbox`, and where its centre line is.

    Returns (axis, centre) with axis "h" or "v", or None when there is no
    wall nearby. `centre` is the wall's mid-line coordinate across that axis,
    in metres.

    A door's swing arc is about as wide as it is deep, so its own bounding
    box says nothing about the wall's direction. The wall itself does. A
    horizontal wall occupies only a few raster rows, so counting wall cells
    per row gives a sharp peak, while counting per column gives a flat
    spread. Whichever profile is more concentrated is the across-wall axis.
    """
    x0, y0, x1, y1 = bbox
    c0, r0 = grid.to_cell([x0 - reach], [y0 - reach])
    c1, r1 = grid.to_cell([x1 + reach], [y1 + reach])
    c0, r0 = max(0, int(c0[0])), max(0, int(r0[0]))
    c1 = min(grid.cols - 1, int(c1[0]))
    r1 = min(grid.rows - 1, int(r1[0]))
    if c1 <= c0 or r1 <= r0:
        return None

    window = mask[r0:r1 + 1, c0:c1 + 1]
    if not window.any():
        return None

    row_counts = window.sum(axis=1).astype(float)
    col_counts = window.sum(axis=0).astype(float)
    total = row_counts.sum()
    if total <= 0:
        return None

    # Concentration: how much of the wall sits in its single busiest line.
    row_focus = row_counts.max() / total
    col_focus = col_counts.max() / total

    if row_focus >= col_focus:
        weights = row_counts
        index = float((weights * range(len(weights))).sum() / weights.sum())
        return "h", grid.y0 + (r0 + index + 0.5) * grid.cell
    weights = col_counts
    index = float((weights * range(len(weights))).sum() / weights.sum())
    return "v", grid.x0 + (c0 + index + 0.5) * grid.cell


# ─────────────────────────────────────────────────────────────────────────────
# Triage: what can be read out of this drawing
# ─────────────────────────────────────────────────────────────────────────────

# Three verdicts, in decreasing order of what the drawing tells us.
#
# `declared` is the ordinary case. The drawing names its own content, with a
# layer that says wall or a block that says chair, and the pipeline reads
# those names.
#
# `recover` is a drawing that names nothing but still holds a real plan. A
# DXF plotted from a PDF is the case this exists for. Every stroke is on one
# meaningless layer, there are no blocks, and a wall is the same line work as
# a desk. Nothing can be read, so everything has to be inferred from the
# geometry and its topology instead.
#
# `refuse` is a drawing with nothing to work from at all.
MODE_DECLARED = "declared"
MODE_RECOVER = "recover"
MODE_REFUSE = "refuse"


class Unreadable(ValueError):
    """A drawing that cannot be turned into a clean plan.

    Subclasses `ValueError` so every existing handler still catches it. It
    carries the triage verdict as well as the message, so the caller can
    offer recovery on a drawing that only needs it, rather than the page
    having to re-parse a 53 MB file to find out.
    """

    def __init__(self, message, verdict):
        super().__init__(message)
        self.verdict = verdict

# Entity types a floor plan is drawn with.
PLAN_GEOMETRY = ("LINE", "LWPOLYLINE", "POLYLINE", "ARC", "CIRCLE",
                 "ELLIPSE", "SPLINE", "HATCH", "SOLID", "3DFACE")
# Entity types that carry words rather than shape. Counted separately: a
# drawing that kept its labels as real text can still name its rooms, which
# is worth knowing before deciding what recovery can achieve.
PLAN_TEXT = ("TEXT", "MTEXT", "ATTRIB", "ATTDEF", "DIMENSION",
             "LEADER", "MLEADER")

# Recovery is statistical. It finds walls from the mode of the gaps between
# parallel faces, furniture from shapes that repeat dozens of times, and
# scale from the most common door swing radius. Every one of those needs a
# population to measure, so a drawing with a few dozen entities cannot be
# recovered however good the method is. An office floor traced from a PDF
# runs to thousands.
RECOVER_MIN_GEOMETRY = 200
# Orientation bins, over 180 degrees. A line has no direction, so a wall at
# 0 and one at 180 are the same bin.
ORIENTATION_BINS = 12
# A bin counts as populated at this share of the straight geometry.
ORIENTATION_MIN_SHARE = 0.05

# Below this scale confidence a recovered plan is told to expect the wrong
# size throughout, and pointed at the measure tool. Recovery scores its scale
# against several anchors at once, so anything under about two thirds means
# the anchors disagreed and no single one of them can be trusted.
RECOVERY_MIN_SCALE_CONFIDENCE = 0.65
# The thickness assumed when a recovered drawing has no measurable wall pair.
# A common internal partition, and the value only sets the raster resolution
# and the wall test's tolerance, both of which degrade gracefully.
RECOVERY_DEFAULT_WALL_M = 0.12


def _orientation_bin(x0, y0, x1, y1):
    """Which orientation bin a straight run falls in, or None if it is a dot."""
    dx, dy = x1 - x0, y1 - y0
    if dx == 0 and dy == 0:
        return None
    angle = math.degrees(math.atan2(dy, dx)) % 180.0
    return min(ORIENTATION_BINS - 1,
               int(angle / (180.0 / ORIENTATION_BINS)))


def assess_readability(doc):
    """Say what can be read out of a drawing, without building anything.

    Returns a verdict dict carrying a `mode` of `declared`, `recover` or
    `refuse`, a `message` written for whoever dropped the file, and the
    `evidence` the mode was decided from.

    This reports rather than raises, because the three outcomes are not one
    yes and one no. A drawing that declares nothing may still hold a
    perfectly good plan, and the caller decides whether to try to recover it.

    Reads the document rather than the flattened items, so it runs before the
    two expensive passes. On the 53 MB file this was written for, that is the
    difference between answering in seconds and answering after five minutes
    of work that was never going to come to anything.
    """
    # It has to be the layers the entities are on, not the layers the file
    # declares. Reading the table instead is much cheaper and was tried:
    # the drawing this check exists for declares 73 layers including
    # `A-Wall`, `A-Door`, `A-Glaz`, `S-Cols` and `EC1 Space Polygons`, and
    # every one of them is empty. They are what was left behind when the
    # model was replaced by a PDF underlay. A layer table says what a
    # drawing once held, not what it holds.
    layers = Counter()
    kinds = Counter()
    texts = 0
    curves = 0
    straight = 0
    orientations = Counter()
    # Counted, not swallowed. A bare `except: pass` around geometry reading
    # hid the hatch bug for the life of that module, so anything unreadable
    # here is reported in the evidence instead of vanishing.
    unreadable = 0
    lo_x = lo_y = float("inf")
    hi_x = hi_y = float("-inf")

    for entity in doc.modelspace():
        kind = entity.dxftype()
        if kind == "INSERT":
            # A block carries a name, so there is something to classify from
            # even where no layer helps. Nothing else needs measuring.
            return {
                "mode": MODE_DECLARED,
                "reason": "blocks",
                "message": "",
                "evidence": {"inserts": "found"},
            }
        if kind in PLAN_TEXT:
            texts += 1
            continue
        if kind not in PLAN_GEOMETRY:
            continue

        kinds[kind] += 1
        layers[entity.dxf.layer if entity.dxf.hasattr("layer") else ""] += 1

        try:
            if kind in ("ARC", "CIRCLE", "ELLIPSE"):
                centre = entity.dxf.center
                radius = float(getattr(entity.dxf, "radius", 0.0) or 0.0)
                if not radius and kind == "ELLIPSE":
                    radius = abs(entity.dxf.major_axis[0]) or 0.0
                points = [(centre[0] - radius, centre[1] - radius),
                          (centre[0] + radius, centre[1] + radius)]
                curves += 1
            elif kind == "SPLINE":
                points = [(p[0], p[1]) for p in entity.control_points]
                curves += 1
            elif kind == "LINE":
                start, end = entity.dxf.start, entity.dxf.end
                points = [(start[0], start[1]), (end[0], end[1])]
            elif kind == "LWPOLYLINE":
                points = [(p[0], p[1]) for p in entity.get_points()]
            elif kind == "POLYLINE":
                points = [(v.dxf.location[0], v.dxf.location[1])
                          for v in entity.vertices]
            else:
                # HATCH, SOLID and 3DFACE all count as geometry, but none is
                # worth the cost of a boundary read for a triage decision.
                continue
        except Exception:
            unreadable += 1
            continue

        if not points:
            unreadable += 1
            continue

        for x, y in points:
            lo_x = min(lo_x, x)
            lo_y = min(lo_y, y)
            hi_x = max(hi_x, x)
            hi_y = max(hi_y, y)

        if kind not in ("ARC", "CIRCLE", "ELLIPSE", "SPLINE"):
            # First point to last. Approximate on a polyline, which is all a
            # triage decision needs.
            index = _orientation_bin(points[0][0], points[0][1],
                                     points[-1][0], points[-1][1])
            if index is not None:
                orientations[index] += 1
                straight += 1

    declared = sorted(name for name in layers
                      if tax.classify_layer_intent(name) in ("structure", "space")
                      or tax.classify_layer_role(name))
    geometry = sum(kinds.values())
    width = (hi_x - lo_x) if hi_x > lo_x else 0.0
    height = (hi_y - lo_y) if hi_y > lo_y else 0.0
    populated = sum(1 for count in orientations.values()
                    if count >= max(1, straight * ORIENTATION_MIN_SHARE))

    evidence = {
        "geometry": geometry,
        "texts": texts,
        "curves": curves,
        "layers": len(layers),
        "layer_names": [name or "(unnamed)"
                        for name, _ in layers.most_common(8)],
        "declared_layers": declared,
        "kinds": dict(kinds.most_common()),
        "orientations": populated,
        "unreadable": unreadable,
        "extent": [round(width, 4), round(height, 4)],
    }

    if declared:
        return {"mode": MODE_DECLARED, "reason": "layers",
                "message": "", "evidence": evidence}
    if not layers:
        # No plan geometry at all. Not this function's problem to report:
        # the flatten step says so in words that fit whatever went wrong.
        return {"mode": MODE_DECLARED, "reason": "nothing to judge",
                "message": "", "evidence": evidence}

    # Nothing in the drawing names anything. Decide whether there is still
    # enough shape to work the plan out from.
    shown = ", ".join(name or "(unnamed)"
                      for name, _ in layers.most_common(4))
    cause = (
        "Nothing in this drawing says what its geometry is. All %d entities "
        "sit on %d layer%s (%s), none of which names walls, rooms or "
        "furniture, and none of them is a block. A DXF converted from a PDF "
        "loses all of that: the plan reads perfectly to a person, but a wall "
        "and a desk are the same line work."
        % (geometry, len(layers), "" if len(layers) == 1 else "s", shown))
    lost = _lost_in_export(doc, layers)

    enough = (geometry >= RECOVER_MIN_GEOMETRY
              and width > 0 and height > 0
              and (populated >= 2 or curves > 0))
    if enough:
        return {
            "mode": MODE_RECOVER,
            "reason": "geometry only",
            "message": cause + " " + lost,
            "recovery": (
                "This drawing does still hold %d pieces of line work across a "
                "%.0f by %.0f area, which is enough to work the plan out from "
                "its shape instead. Build it with recovery turned on to try. "
                "Everything it finds is inferred, not read, so check it."
                % (geometry, width, height)),
            "evidence": evidence,
        }
    return {
        "mode": MODE_REFUSE,
        "reason": "too little geometry",
        "message": (
            cause + " " + lost
            + " There is also too little line work here to work the plan out "
              "from its shape: %d entities in %d orientation%s, where "
              "recovery needs at least %d spread across the plan."
            % (geometry, populated, "" if populated == 1 else "s",
               RECOVER_MIN_GEOMETRY)),
        "evidence": evidence,
    }


def check_classifiable(doc, allow_recovery=False):
    """Raise unless this drawing can be turned into a clean plan.

    The raising wrapper around `assess_readability`, kept because a refusal
    has to stop the build. Returns the verdict when it does not raise, so a
    caller can record which mode it is running in.

    With `allow_recovery` on, a drawing that declares nothing but holds a
    usable plan is allowed through in `recover` mode. Left off, it is
    refused with the reason, because a blank plan under a warning looks like
    a broken tool rather than a broken file.
    """
    verdict = assess_readability(doc)
    if verdict["mode"] == MODE_DECLARED:
        return verdict
    if verdict["mode"] == MODE_RECOVER and allow_recovery:
        return verdict
    message = verdict["message"]
    if verdict["mode"] == MODE_RECOVER:
        message += " " + verdict["recovery"]
    raise Unreadable(message, verdict)


def _lost_in_export(doc, used):
    """Say where the drawing's real content went, if the file shows it.

    A file can still declare the layers it used to draw on. When those are
    the ones a plan is built from and every one of them is empty, the model
    was not converted from a PDF at all: it was dropped, and only a PDF
    underlay came across. That is a fault in the export rather than in the
    drawing, and pointing at it is worth more than a general remark about
    PDFs, because the drawing it came from still has everything.
    """
    generic = "Export from the CAD file the PDF was plotted from instead."
    try:
        declared = [layer.dxf.name for layer in doc.layers]
    except Exception:
        return generic
    empty = [name for name in declared
             if name not in used
             and (tax.classify_layer_intent(name) in ("structure", "space")
                  or tax.classify_layer_role(name))]
    if len(empty) < 3:
        return generic

    # Name the ones a reader will recognise as the missing plan: a room
    # overlay first, because that is the one that carries the room names,
    # then a wall and a door. Alphabetical order picks whatever sorts first
    # and says far less.
    def rank(name):
        if tax.classify_layer_intent(name) == "space":
            return 0
        role = tax.classify_layer_role(name)
        return {"Wall": 1, "Door": 2, "Window": 3}.get(role and role[1], 4)
    telling = sorted(empty, key=lambda name: (rank(name), name))[:3]

    return (
        "This file still declares %d layers a plan could have been built "
        "from (%s) and every one of them is empty, so the export kept the "
        "PDF underlay and dropped the model. Re-export from the drawing "
        "these came from." % (len(empty), ", ".join(telling)))


def structure_features(items):
    """Pull the doors and windows out of the classified items.

    Returns (doors, windows, fragments). `fragments` counts the pieces
    dropped for being too small to be either, which a drawing with its
    blocks exploded produces by the hundred.
    """
    doors, windows = [], []
    fragments = 0
    for item in items:
        if item.element_type != "Structure":
            continue
        if (item.confidence == "layer"
                and item.cls in ("Door",) + tuple(WINDOW_CLASSES)
                and max(item.width, item.height) < MIN_LAYER_FEATURE_M):
            fragments += 1
            continue
        if item.cls == "Door":
            doors.append({
                "id": str(uuid.uuid4()),
                "paths": [list(map(list, path)) for path, _ in item.paths
                          if len(path) >= 2],
                "bbox": item.bbox,
                "center": item.center,
                "block": item.block,
            })
        elif item.cls in WINDOW_CLASSES:
            rings = item.closed_rings()
            if not rings:
                outline = item.outline()
                rings = [outline] if outline else []
            if rings:
                windows.append({
                    "id": str(uuid.uuid4()),
                    "cls": item.cls,
                    "rings": rings,
                    "center": item.center,
                    "block": item.block,
                })
    return doors, windows, fragments


def seal_openings(mask, grid, openings):
    """Return `mask` with every opening filled in, for tracing rooms.

    The wall mass a drawing draws is the wall mass a builder builds: a
    doorway is a hole in it. Rooms have to be traced with those holes
    plugged, or the empty space inside a room is connected to the corridor
    and the two are one region. The original mask is left alone, because
    the openings themselves are exported from it.
    """
    if mask is None or not openings:
        return mask
    plugs = raster.Grid.like(grid)
    raster.rasterise(plugs, [opening["ring"] for opening in openings],
                     closed_fill=True)
    return mask | plugs.mask


def _typical_doorway(openings):
    """The width of this drawing's doorway, in metres.

    Taken from the doors the drawing actually holds rather than assumed,
    because it is used as a length that separates a doorway from a room.
    """
    widths = sorted(opening["width"] for opening in openings
                    if opening.get("width"))
    if not widths:
        return DOORWAY_M
    middle = widths[len(widths) // 2]
    return min(max(middle, DOORWAY_MIN_M), DOORWAY_MAX_M)


def room_mask(mask, grid, openings, wall_thickness):
    """Return the mask to trace rooms against.

    Two steps beyond the wall mass. Each detected doorway is plugged. Then
    the whole thing is closed by half a doorway, which seals every other
    gap of that size: a cased opening with no door leaf drawn, a partition
    that stops short of the mullion, a wall the drawing simply left open.
    Half a doorway is the right amount because it is the largest gap that
    cannot be a room: anything wider is somewhere you can stand.

    Measured on the real drawing this was written for, closing takes the
    largest traced region from 3531 m2 down to 2499 m2 and the rooms that
    find their own label from 26 to 95.
    """
    sealed = seal_openings(mask, grid, openings)
    if sealed is None:
        return sealed
    radius = max(1, int(round(_typical_doorway(openings) / 2.0 / grid.cell)))
    return raster.close_mask(sealed, radius)


def build_openings(doors, mask, grid, wall_thickness):
    """Derive the wall gap each door sits in.

    The reference format carries an `Opening` beside every door: the
    rectangle of wall the door occupies. Its direction and position come
    from the wall the door cuts through, not from the door's own bounding
    box, which for a quarter-circle swing is very nearly square.
    """
    openings = []
    reach = max(wall_thickness * 3.0, 0.3)

    for door in doors:
        bbox = door.get("bbox")
        if not bbox:
            continue
        x0, y0, x1, y1 = bbox
        width, height = x1 - x0, y1 - y0
        if width <= 0 or height <= 0:
            continue
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        half = wall_thickness / 2.0

        found = _wall_axis_near(mask, grid, bbox, reach) if mask is not None else None
        if found:
            axis, centre = found
        else:
            # No wall to align to. Fall back to the longer bbox side.
            axis = "h" if width >= height else "v"
            centre = cy if axis == "h" else cx

        if axis == "h":
            # A swing spans the opening along the wall and the same distance
            # into the room, so the along-wall extent is the door width.
            span = width
            ring = [[cx - span / 2, centre - half], [cx + span / 2, centre - half],
                    [cx + span / 2, centre + half], [cx - span / 2, centre + half]]
        else:
            span = height
            ring = [[centre - half, cy - span / 2], [centre + half, cy - span / 2],
                    [centre + half, cy + span / 2], [centre - half, cy + span / 2]]
        ring.append(list(ring[0]))
        # A stable id, derived from the door it belongs to. Without one the
        # exporter minted a fresh UUID on every write, so two exports of the
        # same unchanged model were never the same file -- and anything
        # keyed on the id saw a new record each time.
        openings.append({"id": "%s:opening" % door["id"], "ring": ring,
                         "door_id": door["id"], "width": round(span, 4),
                         "axis": axis})
    return openings


# ─────────────────────────────────────────────────────────────────────────────
# Model assembly
# ─────────────────────────────────────────────────────────────────────────────

def build_clean_model(dxf_bytes, unit_override=None, prefer_overlay=True,
                      measure_units=True, allow_recovery=False):
    """Build the clean floor-plan model. This is the module's entry point.

    Pass `unit_override` with `measure_units` off to reuse a scale already
    measured on this file and skip the measuring pass, which is the most
    expensive one there is. Wall thickness is then measured from the
    flattened geometry instead.

    Pass `allow_recovery` to build a plan from a drawing that declares
    nothing about its own content, such as a DXF plotted from a PDF. Every
    classification in the result is then inferred from shape rather than read
    from a name, so it is marked as such and reported as such.
    """
    warnings = []
    doc = read_document(dxf_bytes)
    # Before the two expensive passes, not after them.
    verdict = check_classifiable(doc, allow_recovery=allow_recovery)
    recovering = verdict["mode"] == MODE_RECOVER
    if recovering:
        warnings.append(
            "This drawing names none of its own content, so the whole plan "
            "was worked out from its shape. Every class is inferred, not "
            "read. Check it before trusting it.")

    # A recovered drawing needs a scale off a continuous search, not a vote
    # between six standard units. Its factor is whatever the plot was set to,
    # and one real file sits near 1 unit = 10 feet, which no standard unit is
    # within a factor of three of. Deferred import: `fs_recover` needs this
    # module's geometry helpers, and importing it here rather than at the top
    # means neither module has to be half-built while the other loads.
    if recovering and not unit_override:
        import fs_recover
        scale, unit_report = fs_recover.detect_scale(doc)
    else:
        scale, unit_report = fs_units.detect_unit_scale(
            doc, override=unit_override, measure=measure_units)
    if not unit_report.get("header_agrees") and unit_report["source"] == "geometry":
        warnings.append(
            "Drawing header declares %s but the geometry measures as %s. "
            "Used the geometry." % (unit_report.get("header_unit") or "nothing",
                                    unit_report["unit"]))
    if recovering and (unit_report.get("confidence") or 0) < RECOVERY_MIN_SCALE_CONFIDENCE:
        warnings.append(
            "Nothing in this drawing pins its scale down, so the plan may be "
            "the wrong size throughout. Measure a door or a corridor and set "
            "the scale by hand.")

    items, texts = flatten(doc, scale, CURVE_TOLERANCE)
    if not items:
        raise ValueError("No usable geometry found in this DXF.")

    recovery_report = {}
    if recovering:
        import fs_recover
        # The thickness has to be settled before the classification rather
        # than after it, because the wall test measures against it. The
        # ordinary path can leave it until later: it reads classes off
        # layers, which needs no measurement at all.
        thickness = unit_report.get("wall_thickness_m")
        if not thickness or not (fs_units.WALL_MIN <= thickness
                                 <= fs_units.WALL_MAX):
            thickness = RECOVERY_DEFAULT_WALL_M
            warnings.append(
                "Could not measure this drawing's wall thickness, so %.0f mm "
                "was assumed. Walls and rooms may be wrong."
                % (thickness * 1000))
        recovery_report = fs_recover.classify_recovered(
            items, thickness, texts=len(texts))
    else:
        classify_items(items)
    items, texts, off_plan = drop_off_plan(items, texts)
    if off_plan.get("dropped"):
        warnings.append(
            "Ignored %d entities that sit off the floor plate."
            % off_plan["dropped"])

    all_points = [p for item in items for p in item.all_points()]
    bbox = bbox_of(all_points)
    if bbox[2] - bbox[0] <= 0 or bbox[3] - bbox[1] <= 0:
        raise ValueError(
            "This DXF has no measurable floor plan: all its geometry sits on "
            "a single point or line.")
    # Only take the measured thickness if it is one a wall can be. It sets
    # the raster's cell size, so a thickness ten times too small asks for a
    # grid a hundred times too big, and the build stops being one that
    # finishes. `fs_units` is the better measurement of the two and usually
    # right; this is the floor under it.
    measured = unit_report.get("wall_thickness_m")
    if measured and not (fs_units.WALL_MIN <= measured <= fs_units.WALL_MAX):
        warnings.append(
            "Measured the walls as %.0f mm, which is not a thickness a wall "
            "has. Measured them again from the flattened geometry."
            % (measured * 1000))
        measured = None
    wall_thickness = measured or estimate_wall_thickness(items)

    # Recover objects from loose fragments, now that the thickness has been
    # measured from the individual bands. Order matters: grouping first would
    # leave nothing band-shaped to measure.
    items, group_report = group_loose_geometry(items)
    if recovering:
        import fs_recover
        # The earliest point at which a recovered object has a footprint to
        # measure, so this is where its size rules and its repeated shapes
        # can finally speak.
        recovery_report["furniture"] = fs_recover.refine_recovered(items)
    else:
        classify_grouped(items)
    if group_report["grouped"]:
        warnings.append(
            "This drawing has its blocks exploded. Joined %d loose fragments "
            "into %d objects by proximity."
            % (group_report["grouped"], group_report["into"]))

    zone_rings, items = take_floor_zone(items, bbox)
    wall_polygons, grid, mask, footprint = build_walls(
        items, bbox, wall_thickness, zone_rings)
    if not wall_polygons:
        warnings.append("No wall geometry could be identified.")
    if grid.cell > grid.asked_cell * 1.05:
        # The plan is bigger than the resolution the wall thickness asks
        # for, so the raster was coarsened to something that can finish.
        # Say so: at this cell size a thin partition can be missed.
        warnings.append(
            "This drawing is too large to trace at %.0f mm, so it was traced "
            "at %.0f mm instead. Thin partitions may be missed."
            % (grid.asked_cell * 1000, grid.cell * 1000))

    # ---- structure features --------------------------------------------
    # Read before the spaces, because the doors are what close the rooms.
    doors, windows, fragments = structure_features(items)
    openings = build_openings(doors, mask, grid, wall_thickness)

    # ---- spaces ---------------------------------------------------------
    spaces, overlay_report = ([], {}) if not prefer_overlay \
        else spaces_from_overlay(items, texts)
    space_source = "overlay"
    if not spaces:
        # Trace against a mask with every doorway plugged and every other
        # doorway-sized gap closed. A room is only enclosed if its door is
        # shut: traced against the wall mass as drawn, the empty space flows
        # out through every doorway and the whole floor comes back as one
        # room holding every label in the drawing.
        spaces = spaces_from_walls(
            room_mask(mask, grid, openings, wall_thickness),
            grid, wall_thickness, texts)
        spaces = label_traced_spaces(spaces, texts)
        space_source = "traced"
        if not spaces:
            warnings.append("No enclosed spaces could be identified.")
        else:
            leak = max(space["area"] for space in spaces)
            if leak > MAX_ROOM_AREA:
                warnings.append(
                    "The drawing's partitions do not close, so %.0f m2 of it "
                    "came back as one space. Check the wall layers."
                    % leak)

    # Fill in whatever the overlay left blank, so no part of the floor and no
    # piece of furniture belongs to nothing.
    if space_source == "overlay" and spaces:
        extra = residual_spaces(spaces, mask, grid, footprint, wall_thickness)
        if extra:
            spaces = spaces + extra
            report_residual = len(extra)
        else:
            report_residual = 0
    else:
        report_residual = 0

    for space in spaces:
        label_text = " ".join(t["text"] for t in space["labels"]).strip()
        if space["source"] == "residual" and not label_text:
            cls, sub = classify_residual(space)
            confidence = "shape"
        else:
            cls, sub, confidence = tax.classify_space(label_text, space["area"])
        space["id"] = str(uuid.uuid4())
        space["label"] = label_text
        space["cls"] = cls
        space["sub_class"] = sub
        space["label_confidence"] = confidence
        space["name"] = _space_name(space["labels"])


    # ---- furniture and decor -------------------------------------------
    furniture = []
    for item in items:
        if item.element_type not in ("Furniture", "Decor"):
            continue
        outline = item.outline()
        if outline is None or ring_area(outline) <= 0:
            continue
        furniture.append({
            "id": str(uuid.uuid4()),
            "element_type": item.element_type,
            "cls": item.cls,
            "sub_class": item.sub_class,
            "confidence": item.confidence,
            "rings": [outline],
            "center": ring_centroid(outline),
            "area": ring_area(outline),
            "block": tax.clean_block_name(item.block),
            "space_id": None,
            "capacity": tax.seats_for(item.sub_class),
        })

    assign_to_spaces(furniture, spaces)
    refined = refine_by_space(furniture, spaces)
    # A bench of six desks is one shape in the drawing and six desks on the
    # floor. Split before the capacity and the desk numbers are worked out,
    # or both count the bench once.
    furniture, split_report = split_desk_runs(furniture)
    if split_report["runs"]:
        warnings.append(
            "Split %d bench runs into %d desks, at the %.0f mm desk pitch "
            "measured from this drawing. Each desk in a run is inferred from "
            "that pitch, not drawn."
            % (split_report["runs"], split_report["into"],
               split_report["pitch_m"] * 1000))
    if recovering:
        import fs_recover
        # A drawing that named nothing named none of its rooms either, so
        # what stands in a room is the only thing left that says what it is.
        # Runs after `assign_to_spaces`, which is what puts a piece in a room.
        recovery_report["spaces"] = fs_recover.classify_by_contents(
            spaces, furniture)
    compute_space_capacity(spaces, furniture)
    assign_desk_ids(furniture)

    # ---- the floor slab -------------------------------------------------
    # The plan view paints this under everything as the floor's own ground,
    # so a corridor with no space polygon on it still reads as part of the
    # building rather than as a hole in it.
    floor = []
    if footprint is not None and footprint.any():
        floor = raster.trace_polygons(
            footprint, grid, simplify=grid.cell * 2.0,
            min_area_cells=max(4, int(MIN_FLOOR_PIECE_M2 / (grid.cell ** 2))))

    model = {
        "unit": unit_report,
        "scale": scale,
        "wall_thickness": round(wall_thickness, 4),
        "floor": floor,
        "walls": wall_polygons,
        "doors": doors,
        "windows": windows,
        "openings": openings,
        "spaces": spaces,
        "furniture": furniture,
        "warnings": warnings,
        "report": {
            "mode": verdict["mode"],
            "mode_reason": verdict["reason"],
            "readability": verdict["evidence"],
            "recovery": recovery_report,
            "space_source": space_source,
            "floor_outline_source": "zone polygon" if zone_rings
                                    else "derived from footprint",
            "residual_spaces": report_residual,
            "grouping": group_report,
            "refined_by_space": refined,
            "desk_runs": split_report,
            "feature_fragments_dropped": fragments,
            "overlay": overlay_report,
            "off_plan": off_plan,
            "raster_cell": round(grid.cell, 4),
            "floor_pieces": len(floor),
            "items": len(items),
            "texts": len(texts),
        },
    }
    recentre(model)
    model["stats"] = summarise(model)
    return model


def _space_name(labels):
    """Pick a display name from a space's labels, if one reads like a name."""
    for label in labels:
        text = label["text"]
        # A short code with a digit is a room name; a taxonomy word is not.
        if text and any(ch.isdigit() for ch in text) and len(text) <= 24:
            return text
    return None


def assign_to_spaces(furniture, spaces):
    """Set each furniture item's `space_id` to the space that contains it.

    Smallest containing space wins, so a chair inside a meeting room inside
    an open-plan zone belongs to the meeting room.
    """
    if not spaces:
        return
    ordered = sorted(spaces, key=lambda s: s["area"])
    # Bucket spaces by a coarse grid so each item tests only nearby ones.
    grid_size = max(1.0, math.sqrt(max(s["area"] for s in spaces)) / 2.0)
    buckets = defaultdict(list)
    for space in ordered:
        x0, y0, x1, y1 = bbox_of(space["rings"][0])
        for gx in range(int(x0 // grid_size), int(x1 // grid_size) + 1):
            for gy in range(int(y0 // grid_size), int(y1 // grid_size) + 1):
                buckets[(gx, gy)].append(space)

    for piece in furniture:
        cx, cy = piece["center"]
        for space in buckets.get((int(cx // grid_size), int(cy // grid_size)), ()):
            if point_in_ring((cx, cy), space["rings"][0]):
                piece["space_id"] = space["id"]
                break


def refine_by_space(furniture, spaces):
    """Correct a table's sub-class from the room it stands in.

    Only touches pieces named by their size, never one named by its block:
    a drawing that called the shape a conference table has said more than a
    measurement can. A table's own dimensions cannot separate a boardroom
    table from a bench run of desks, but the room can.
    """
    by_id = {space["id"]: space for space in spaces}
    changed = promoted = 0
    for piece in furniture:
        space = by_id.get(piece["space_id"])
        if not space:
            continue
        wanted = tax.TABLE_BY_SPACE.get(space["cls"])
        if not wanted:
            continue

        if piece["confidence"] == "size" and piece["cls"] == "Table":
            if wanted != piece["sub_class"]:
                piece["sub_class"] = wanted
                piece["confidence"] = "size+space"
                changed += 1
        elif (piece["cls"] == "Other"
                and space["cls"] in PROMOTE_TABLE_SPACES
                and _table_like(piece)):
            # A drawing with its blocks exploded leaves the main table in
            # most rooms unnamed and unnameable by size alone, because a
            # rectangle 4 m long could be a table, a counter or a run of
            # lockers. In a workspace or a meeting room it is a table, and
            # nothing else that shape belongs there.
            piece["cls"], piece["sub_class"] = "Table", wanted
            piece["confidence"] = "space"
            promoted += 1
    return {"retyped": changed, "promoted": promoted}


# Footprint bounds, in metres, for calling an unnamed shape a table. Only
# applied inside the space classes below, which is what keeps a 6 m run of
# lockers in a store and a serving counter in a kitchen out of it.
PROMOTE_TABLE_LONG = (0.9, 8.0)
PROMOTE_TABLE_SHORT = (0.5, 3.0)
PROMOTE_TABLE_SPACES = ("Work", "Meet")

# ─────────────────────────────────────────────────────────────────────────────
# Splitting a bench run into its desks
# ─────────────────────────────────────────────────────────────────────────────
#
# A bench of six desks shares one spine, so its desks touch, so a drawing
# with its blocks exploded gives the whole bench as one shape. The size rules
# and `refine_by_space` both then call that shape a desk, which is right
# about what it is and wrong about how many.
#
# It matters because of what the tool is for. Desk numbering, IoT assignment
# and every capacity figure count desk objects, so a bench of six counted
# once undercounts the floor by five. Measured on the third real file: 144
# desks reported, of which only 53 were a single desk's length. The other 91
# were runs of two to five.
#
# The pitch is measured from the drawing rather than assumed. A floor's desks
# are all the same size, and the single desks the size rules did recognise
# say what that size is.

# A single desk, for the purpose of measuring the drawing's own pitch. The
# same bounds the size rules use for one desk.
DESK_PITCH_RANGE = (1.15, 2.05)
DESK_DEPTH_RANGE = (0.55, 0.95)
# Used only when a drawing holds no single desk to measure. A common desk.
DESK_PITCH_FALLBACK = 1.6
DESK_DEPTH_FALLBACK = 0.8
# Below this many pitches long, a run is one desk with some slack around it
# rather than two desks. Well below 2, because a bench of two drawn tight is
# a little under twice one desk.
SPLIT_MIN_RATIO = 1.6
# A run this far off a rectangle is not split. The pieces are cut out of the
# bounding box, so a shape that does not fill its box would put desks in
# empty floor. An L of benches traced as one hull is the case this refuses.
SPLIT_MIN_RECTANGULARITY = 0.75
# No run yields more than this many desks. A guard against a mis-scaled
# drawing turning one shape into hundreds of them.
SPLIT_MAX_PIECES = 40
# How near two desks must be to count as the same bench when a correction is
# applied in bulk. Desks cut from one run share their edges exactly, so any
# positive value keeps a bench together; two separate benches are parted by a
# walkway, which is metres. 0.2 m sits far below the narrowest aisle anyone
# builds and far above the rounding on a shared edge, so it is not a
# threshold the answer is sensitive to.
DESK_GROUP_GAP = 0.20
# How closely two benches must measure to count as the same bench repeated.
# Relative, on each side of the fitted rectangle. Benches on a floor are one
# component ordered many times over, so real copies agree to well inside
# this; 5% is loose enough to absorb a drawing where a run was traced a
# little wide.
DESK_MATCH_TOLERANCE = 0.05


def desk_pitch(furniture):
    """The drawing's own desk size, as (pitch, depth) in metres.

    Measured from the pieces the size rules already recognised as one desk,
    because a floor's desks are all the same size and the drawing is the only
    thing that knows what that size is. Falls back to a common desk when a
    drawing holds none, which happens on a floor that is all bench.
    """
    lengths, depths = [], []
    for piece in furniture:
        if piece.get("sub_class") != "Desk":
            continue
        fitted = min_area_rect(piece["rings"][0])
        if not fitted:
            continue
        long_side, short_side = fitted[0], fitted[1]
        if (DESK_PITCH_RANGE[0] <= long_side <= DESK_PITCH_RANGE[1]
                and DESK_DEPTH_RANGE[0] <= short_side <= DESK_DEPTH_RANGE[1]):
            lengths.append(long_side)
            depths.append(short_side)
    if len(lengths) >= 4:
        lengths.sort()
        depths.sort()
        # Plain floats. These reach the report, the report crosses into
        # JavaScript as JSON, and a numpy scalar does not always survive
        # that: `float64` happens to subclass `float` and get through, and
        # `int64` does not. The same rule as the unit report keeps to.
        return (float(lengths[len(lengths) // 2]),
                float(depths[len(depths) // 2]))
    return DESK_PITCH_FALLBACK, DESK_DEPTH_FALLBACK


def cut_grid_ring(points, across, down):
    """Cut the minimum-area rectangle around `points` into an `across` by
    `down` grid of smaller rectangles, in world coordinates.

    Returns a list of `across * down` closed rings, or `[]` if `points` has
    no valid oriented rectangle (fewer than 3 non-collinear points).

    Shared by `split_desk_runs`, which picks `across`/`down` itself from a
    pitch measured across the whole file, and `resplit_desks`, which takes
    them from a person who can see the real drawing -- the cut itself is the
    same rectangle-into-cells arithmetic either way.
    """
    fitted = min_area_rect(points)
    if not fitted:
        return []
    long_side, short_side, angle, (cx, cy) = fitted
    if long_side <= 0 or short_side <= 0:
        return []

    step_x, step_y = long_side / across, short_side / down
    cos, sin = math.cos(angle), math.sin(angle)

    def to_world(local_x, local_y):
        """A point in the run's frame, put back where it belongs."""
        return (cx + local_x * cos - local_y * sin,
                cy + local_x * sin + local_y * cos)

    cells = []
    for row in range(down):
        for column in range(across):
            ax = -long_side / 2.0 + column * step_x
            ay = -short_side / 2.0 + row * step_y
            cells.append(close_ring([to_world(ax, ay),
                                     to_world(ax + step_x, ay),
                                     to_world(ax + step_x, ay + step_y),
                                     to_world(ax, ay + step_y)]))
    return cells


def _desk_pieces_from_cells(cells, template):
    """One furniture dict per cell, copied from `template` except geometry."""
    parts = []
    for cell in cells:
        part = dict(template)
        part["id"] = str(uuid.uuid4())
        part["rings"] = [cell]
        part["center"] = ring_centroid(cell)
        part["area"] = ring_area(cell)
        # Each desk seats one person. Left as the run's own capacity this
        # would still say the bench holds one.
        part["capacity"] = tax.seats_for(template.get("sub_class"))
        # The run was one object in the drawing, and each desk in it is
        # inferred rather than drawn. Say so.
        part["split_from_run"] = True
        parts.append(part)
    return parts


def split_desk_runs(furniture):
    """Replace each bench run with the desks it holds. Returns a report.

    Runs after `refine_by_space`, so every desk-like shape has settled on
    being a desk, and before `compute_space_capacity` and `assign_desk_ids`,
    so the capacity and the desk numbers are counted per desk.

    A run is cut on both axes. A bench drawn back to back is two rows deep as
    well as several desks long, and 6.4 by 1.6 m at a 1.6 by 0.8 m desk is
    eight desks rather than four.

    One pitch is measured for the whole file (`desk_pitch`), so a bench whose
    desks are genuinely a different size to the rest of the floor -- a real
    case, not a hypothetical, see "A bench run can still split into the wrong
    number of desks" -- can still come out at the wrong count. `resplit_desks`
    is the by-hand correction for exactly that, once a person can see the
    source drawing and this pass could not.
    """
    pitch, depth = desk_pitch(furniture)
    report = {"pitch_m": round(pitch, 3), "depth_m": round(depth, 3),
              "runs": 0, "into": 0}
    out = []
    for piece in furniture:
        if piece.get("sub_class") != "Desk":
            out.append(piece)
            continue
        ring = piece["rings"][0]
        fitted = min_area_rect(ring)
        if not fitted:
            out.append(piece)
            continue
        # The run's own extent and angle, not the world's. A bench in a
        # building drawn at 31 degrees has an axis-aligned box far bigger
        # than the bench, and slicing that box would cut across the desks
        # rather than between them.
        long_side, short_side, angle, (cx, cy) = fitted
        if long_side <= 0 or short_side <= 0:
            out.append(piece)
            continue
        # Cut out of that rectangle, so the shape has to fill it. A hull
        # around an L of benches does not, and slicing that would put desks
        # on empty floor.
        if ring_area(ring) < SPLIT_MIN_RECTANGULARITY * long_side * short_side:
            out.append(piece)
            continue

        # How many desks along each axis of the run itself.
        across = max(1, int(round(long_side / pitch)))
        down = max(1, int(round(short_side / depth)))
        if long_side < SPLIT_MIN_RATIO * pitch:
            across = 1
        if short_side < SPLIT_MIN_RATIO * depth:
            down = 1
        if across * down <= 1 or across * down > SPLIT_MAX_PIECES:
            out.append(piece)
            continue

        cells = cut_grid_ring(ring, across, down)
        if not cells:
            out.append(piece)
            continue
        out.extend(_desk_pieces_from_cells(cells, piece))
        report["runs"] += 1
        report["into"] += across * down

    return out, report


def desk_groups(pieces, gap=DESK_GROUP_GAP):
    """Cluster furniture pieces into the benches they belong to.

    Returns a list of lists, each holding the pieces of one bench, ordered
    top-left first so a caller reporting them reads them in the same order
    twice running.

    Desks cut from one run share their edges, and two benches are parted by a
    walkway, so proximity separates them cleanly -- the same reasoning
    `group_loose_geometry` uses on a drawing's fragments, one level up.

    This is what lets a correction be applied in bulk. Pooling every selected
    desk into one rectangle is right for one bench and wrong for nine: it
    would cut a single grid across the whole floor, aisles included.
    """
    if not pieces:
        return []
    boxes = [bbox_of(piece["rings"][0]) for piece in pieces]
    groups = [[pieces[index] for index in group]
              for group in _bbox_proximity_groups(boxes, gap)]
    groups.sort(key=lambda g: (-round(max(p["center"][1] for p in g), 1),
                               round(min(p["center"][0] for p in g), 1)))
    return groups


def _group_footprint(group):
    """The fitted rectangle around a whole bench, as (long, short).

    Returns None when the pooled points have no valid rectangle.
    """
    points = [p for piece in group for ring in piece["rings"] for p in ring]
    fitted = min_area_rect(points)
    if not fitted or fitted[0] <= 0 or fitted[1] <= 0:
        return None
    return fitted[0], fitted[1]


def matching_desk_groups(model, ids, tolerance=DESK_MATCH_TOLERANCE,
                         gap=DESK_GROUP_GAP):
    """Every bench on the plan built like the one the given pieces belong to.

    Returns (id_lists, reference_report). `id_lists` holds one list of piece
    ids per matching bench, the reference bench included, so a caller can
    select them all and correct them in one go.

    A floor's benches are one component ordered many times, so the wrong
    split is never wrong once -- the report that prompted this said "pretty
    much all of them". Matching is on the fitted footprint *and* the current
    piece count: a bench the same size that came out at a different count was
    cut differently, and sweeping it up in a correction aimed at this one
    would be changing something nobody looked at.

    The footprint is the bench's own fitted rectangle, not its axis-aligned
    box, so this works on a building drawn at an angle -- the same reason
    `classify_by_size` reads `min_area_rect`.
    """
    desks = [f for f in model.get("furniture", ())
             if f.get("sub_class") == "Desk" and f.get("rings")]
    id_set = set(ids)
    groups = desk_groups(desks, gap)

    reference = [g for g in groups if any(p["id"] in id_set for p in g)]
    if not reference:
        raise ValueError("None of the selected pieces are on the plan any more.")
    if len(reference) > 1:
        raise ValueError("Select one bench to match against, not %d."
                         % len(reference))
    reference = reference[0]
    size = _group_footprint(reference)
    if not size:
        raise ValueError("Could not fit a rectangle to the selected bench.")

    long_side, short_side = size
    matches = []
    for group in groups:
        if len(group) != len(reference):
            continue
        other = _group_footprint(group)
        if not other:
            continue
        if (abs(other[0] - long_side) <= tolerance * long_side
                and abs(other[1] - short_side) <= tolerance * short_side):
            matches.append([piece["id"] for piece in group])

    return matches, {"pieces": len(reference),
                     "long_m": round(long_side, 3),
                     "short_m": round(short_side, 3)}


def resplit_desks(model, ids, across, down, gap=DESK_GROUP_GAP):
    """Re-cut the benches the given pieces belong to into an `across` by
    `down` grid each. Mutates `model["furniture"]` in place.

    The manual half of the fix `split_desk_runs`'s docstring points to: a
    person looking at the source drawing gives the real count for one bench,
    rather than the pipeline guessing it from a pitch measured over the
    whole file. Works the same whether `ids` is several already-split desks
    that should have been fewer, or one un-split run that never passed
    `SPLIT_MIN_RECTANGULARITY` -- both are just a set of pieces to pool the
    points of and cut again.

    **The selection is clustered into benches first**, and each bench is cut
    to the stated grid on its own. A floor gets the same bench wrong the same
    way many times over, so correcting them one at a time is the wrong unit
    of work; pooling them all into one rectangle instead would cut a single
    grid across the floor, aisles and all. Selecting one bench is the same
    operation with one cluster in it, which is why this subsumes the
    single-bench case rather than sitting beside it.

    Desk numbers are not reassigned here. `assign_desk_ids` sorts every desk
    in the model by position, so re-running it after a correction would
    renumber every other desk on the floor as a side effect. The new pieces
    carry no `custom_id` until the next full build. That holds for a bulk run
    too: a hundred corrected desks still leave the rest of the floor's
    numbering alone.

    Returns (removed_ids, new_pieces, report). `report` carries how many
    benches were cut and how many were skipped, because a bench whose pooled
    points have no valid rectangle is passed over rather than aborting the
    other ninety-nine -- and a silent skip is exactly the kind of thing this
    file has been bitten by. Raises ValueError if none of `ids` are still on
    the plan, or if no bench at all could be cut.
    """
    id_set = set(ids)
    furniture = model["furniture"]
    selected = [f for f in furniture if f["id"] in id_set]
    if not selected:
        raise ValueError("None of the selected pieces are on the plan any more.")

    groups = desk_groups(selected, gap)
    new_pieces, removed_ids, skipped = [], [], 0
    for group in groups:
        points = [p for piece in group for ring in piece["rings"] for p in ring]
        cells = cut_grid_ring(points, across, down)
        if not cells:
            skipped += 1
            continue
        template = dict(group[0])
        template["custom_id"] = None
        new_pieces.extend(_desk_pieces_from_cells(cells, template))
        removed_ids.extend(piece["id"] for piece in group)

    if not new_pieces:
        raise ValueError("Could not fit a rectangle to the selected pieces.")

    report = {"benches": len(groups) - skipped, "skipped": skipped,
              "across": across, "down": down}
    cut_ids = set(removed_ids)
    model["furniture"] = [f for f in furniture if f["id"] not in cut_ids] + new_pieces
    compute_space_capacity(model["spaces"], model["furniture"])
    model["stats"] = summarise(model)
    return removed_ids, new_pieces, report


def _table_like(piece):
    """True when a piece's footprint could be a table."""
    ring = piece["rings"][0]
    x0, y0, x1, y1 = bbox_of(ring)
    long_side, short_side = max(x1 - x0, y1 - y0), min(x1 - x0, y1 - y0)
    return (PROMOTE_TABLE_LONG[0] <= long_side <= PROMOTE_TABLE_LONG[1]
            and PROMOTE_TABLE_SHORT[0] <= short_side <= PROMOTE_TABLE_SHORT[1])


def compute_space_capacity(spaces, furniture):
    """Count the seats inside each space.

    Circulation and support spaces take no capacity, matching how the
    reference leaves a corridor at zero.
    """
    seats = Counter()
    for piece in furniture:
        if piece["space_id"] and piece.get("capacity"):
            seats[piece["space_id"]] += piece["capacity"]

    for space in spaces:
        if space["sub_class"] == "Workpoint":
            space["capacity"] = 1
            continue
        if space["cls"] in tax.NO_CAPACITY_CLASSES:
            space["capacity"] = 0
            continue
        space["capacity"] = int(seats.get(space["id"], 0))


def assign_desk_ids(furniture):
    """Number the desks in reading order, as FM001 upwards.

    Matches the reference file, which carries a customId of that shape on
    every desk and on nothing else.
    """
    desks = [f for f in furniture if f["sub_class"] == "Desk"]
    desks.sort(key=lambda f: (-round(f["center"][1], 1), round(f["center"][0], 1)))
    for index, desk in enumerate(desks, start=1):
        desk["custom_id"] = "FM%03d" % index


def all_rings(model):
    """Yield every ring in a model, in place, so a caller can move points.

    One place that knows where the coordinates live. Miss a field here and a
    transform leaves part of the plan behind, which is the kind of fault that
    looks like a rendering glitch and is really a broken model.
    """
    for polygon in model.get("floor") or ():
        for ring in polygon:
            yield ring
    for polygon in model["walls"]:
        for ring in polygon:
            yield ring
    for space in model["spaces"]:
        for ring in space["rings"]:
            yield ring
    for piece in model["furniture"]:
        for ring in piece["rings"]:
            yield ring
    for door in model["doors"]:
        for path in door["paths"]:
            yield path
    for window in model["windows"]:
        for ring in window["rings"]:
            yield ring
    for opening in model["openings"]:
        yield opening["ring"]


def rotate(model, turns):
    """Rotate a model a quarter turn at a time about its own centre.

    Positive `turns` is anticlockwise. The app rotates the elements it draws
    and calls this so the exported file agrees with what is on screen: a plan
    turned north-up must export north-up, or the picture and the file are two
    different drawings.

    Only quarter turns, which is why the door bounding boxes can simply be
    re-derived from their rotated corners and the opening axes swapped.
    """
    turns = int(turns) % 4
    if not turns:
        return model

    points = [p for ring in all_rings(model) for p in ring]
    if not points:
        return model
    x0, y0, x1, y1 = bbox_of(points)
    px, py = (x0 + x1) / 2.0, (y0 + y1) / 2.0

    angle = turns * math.pi / 2.0
    cos_a, sin_a = math.cos(angle), math.sin(angle)

    def turn(x, y):
        dx, dy = x - px, y - py
        return [px + dx * cos_a - dy * sin_a, py + dx * sin_a + dy * cos_a]

    for ring in all_rings(model):
        for point in ring:
            point[0], point[1] = turn(point[0], point[1])

    for group in ("spaces", "furniture", "doors", "windows"):
        for item in model.get(group) or ():
            if item.get("center"):
                item["center"] = turn(item["center"][0], item["center"][1])

    for door in model["doors"]:
        if door.get("bbox"):
            b = door["bbox"]
            corners = [turn(b[0], b[1]), turn(b[2], b[1]),
                       turn(b[2], b[3]), turn(b[0], b[3])]
            door["bbox"] = (min(c[0] for c in corners), min(c[1] for c in corners),
                            max(c[0] for c in corners), max(c[1] for c in corners))

    if turns % 2:
        for opening in model["openings"]:
            opening["axis"] = "y" if opening.get("axis") == "x" else "x"

    points = [p for ring in all_rings(model) for p in ring]
    model["bbox"] = list(bbox_of(points))
    model["stats"] = summarise(model)
    model["rotation"] = ((model.get("rotation") or 0) + turns) % 4
    return model


# Groups of the model that carry an id, and so can be addressed by an edit
# made on the canvas. Walls, openings and the floor slab are given synthetic
# ids by `fs_bridge`, so they are here too.
EDITABLE_GROUPS = ("spaces", "furniture", "doors", "windows")


def _item_rings(item):
    """Every ring of one model item, whatever shape it keeps them in."""
    if "rings" in item:
        return item["rings"]
    if "paths" in item:
        return item["paths"]
    if "ring" in item:
        return [item["ring"]]
    return []


def apply_edits(model, present, offsets):
    """Move and hide model items to match what is on the canvas.

    `present` is the set of ids still on the plan; anything else has been
    deleted. `offsets` maps an id to the total move applied to it, in
    metres.

    Both are absolute rather than incremental, and the applied state is
    recorded on each item, so calling this twice does nothing the second
    time and undoing a move puts the item back. A deletion hides the item
    rather than dropping it, for the same reason: an undone deletion has to
    be able to come back.

    Returns a report of what changed.
    """
    moved = hidden = restored = 0
    for group in EDITABLE_GROUPS:
        for item in model.get(group) or ():
            item_id = item.get("id")
            if item_id is None:
                continue

            was_hidden = bool(item.get("_hidden"))
            now_hidden = item_id not in present
            if now_hidden != was_hidden:
                item["_hidden"] = now_hidden
                if now_hidden:
                    hidden += 1
                else:
                    restored += 1

            wanted = offsets.get(item_id) or (0.0, 0.0)
            applied = item.get("_applied_offset") or (0.0, 0.0)
            dx, dy = wanted[0] - applied[0], wanted[1] - applied[1]
            if abs(dx) < 1e-12 and abs(dy) < 1e-12:
                continue
            for ring in _item_rings(item):
                for point in ring:
                    point[0] += dx
                    point[1] += dy
            if item.get("center"):
                item["center"] = [item["center"][0] + dx,
                                  item["center"][1] + dy]
            if item.get("bbox"):
                b = item["bbox"]
                item["bbox"] = (b[0] + dx, b[1] + dy, b[2] + dx, b[3] + dy)
            item["_applied_offset"] = [wanted[0], wanted[1]]
            moved += 1

    return {"moved": moved, "hidden": hidden, "restored": restored}


def recentre(model):
    """Shift the plan so its bounding box is centred on the origin.

    The reference file is centred this way. Keeping to it means the exported
    plan lands in the same place, rather than tens of kilometres out at
    whatever coordinates the drawing happened to use.
    """
    points = []
    for polygon in model["walls"]:
        for ring in polygon:
            points.extend(ring)
    for polygon in model.get("floor") or ():
        for ring in polygon:
            points.extend(ring)
    for space in model["spaces"]:
        for ring in space["rings"]:
            points.extend(ring)
    for piece in model["furniture"]:
        points.extend(piece["rings"][0])
    for door in model["doors"]:
        for path in door["paths"]:
            points.extend(path)
    if not points:
        return

    x0, y0, x1, y1 = bbox_of(points)
    dx, dy = -(x0 + x1) / 2.0, -(y0 + y1) / 2.0
    # Kept so a caller can map a model coordinate back to the drawing.
    model["origin_shift"] = [dx, dy]

    def move_ring(ring):
        for point in ring:
            point[0] += dx
            point[1] += dy

    for polygon in model["walls"]:
        for ring in polygon:
            move_ring(ring)
    for polygon in model.get("floor") or ():
        for ring in polygon:
            move_ring(ring)
    for space in model["spaces"]:
        for ring in space["rings"]:
            move_ring(ring)
        space["center"] = [space["center"][0] + dx, space["center"][1] + dy]
    for piece in model["furniture"]:
        for ring in piece["rings"]:
            move_ring(ring)
        piece["center"] = [piece["center"][0] + dx, piece["center"][1] + dy]
    for door in model["doors"]:
        for path in door["paths"]:
            move_ring(path)
        if door.get("center"):
            door["center"] = [door["center"][0] + dx, door["center"][1] + dy]
        if door.get("bbox"):
            b = door["bbox"]
            door["bbox"] = (b[0] + dx, b[1] + dy, b[2] + dx, b[3] + dy)
    for window in model["windows"]:
        for ring in window["rings"]:
            move_ring(ring)
        if window.get("center"):
            window["center"] = [window["center"][0] + dx, window["center"][1] + dy]
    for opening in model["openings"]:
        move_ring(opening["ring"])

    model["offset"] = [dx, dy]
    model["bbox"] = [x0 + dx, y0 + dy, x1 + dx, y1 + dy]


def summarise(model):
    """Counts and totals for the UI and for regression checks."""
    space_types = Counter((s["cls"], s["sub_class"]) for s in model["spaces"])
    furniture_types = Counter(
        (f["element_type"], f["cls"], f["sub_class"]) for f in model["furniture"])
    confidence = Counter(f["confidence"] for f in model["furniture"])

    bbox = model.get("bbox") or [0, 0, 0, 0]
    return {
        "floor_size_m": [round(bbox[2] - bbox[0], 2), round(bbox[3] - bbox[1], 2)],
        "wall_polygons": len(model["walls"]),
        "wall_rings": sum(len(p) for p in model["walls"]),
        "doors": len(model["doors"]),
        "windows": len(model["windows"]),
        "openings": len(model["openings"]),
        "spaces": len(model["spaces"]),
        # A Workpoint is a per-desk allocation nested inside its containing
        # zone (see fs_taxonomy's note on the term), so summing every space's
        # area double-counts that zone's floor once for itself and again for
        # every desk allocation drawn inside it. Workpoints are the one nested
        # level this pipeline produces; excluding them picks the outer level.
        "space_area_m2": round(
            sum(s["area"] for s in model["spaces"] if s["sub_class"] != "Workpoint"), 1),
        "furniture": len(model["furniture"]),
        "seats": sum(f.get("capacity") or 0 for f in model["furniture"]),
        "desks": sum(1 for f in model["furniture"] if f["sub_class"] == "Desk"),
        "space_types": {"%s / %s" % k: v for k, v in space_types.most_common()},
        "furniture_types": {"%s / %s / %s" % k: v
                            for k, v in furniture_types.most_common()},
        "classification": dict(confidence),
        # The share the pipeline worked out, rather than read off a layer.
        # Computed here so the page does not have to know which confidence
        # values count, which is how the figure silently went wrong before:
        # it added up the values it knew about, so every new one the pipeline
        # learned to produce dropped out of the total.
        "identified": round(
            100.0 * sum(count for value, count in confidence.items()
                        if value not in CONFIDENCE_VAGUE)
            / max(1, sum(confidence.values())), 1),
    }
