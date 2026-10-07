"""
Raster reconstruction of floor-plan topology.
=============================================

A DXF draws a wall as two parallel lines, or four, or as a hatch, or as a
closed band, or as a block full of loose fragments. All five appear in the
same file. Reconstructing walls by pairing lines up geometrically means
handling every one of those cases and every way they can be broken.

Rasterising sidesteps that. Draw all the structural ink into a bitmap, close
the small gaps so each wall's own faces fuse into one solid band, then trace
the outline of what remains. The result is one clean wall mass, whatever the
source drawing did. The same bitmap answers the other hard question for free:
the enclosed rooms are the connected empty regions inside the wall mass.

Everything here works in metres and returns metres. Only numpy is required,
which the bundled Pyodide runtime already carries.

Main entry points:
    `rasterise`            ink to bitmap
    `close_mask`           fuse parallel wall faces into one band
    `trace_polygons`       bitmap to GeoJSON-ready rings
    `label_regions`        connected empty regions, for room detection
    `nearest_seed_labels`  split one region by which seed point is closest
"""

from __future__ import annotations

import numpy as np

# Cap on grid size, as total cells. A 52 m by 41 m floor at 25 mm resolution
# is about 3.4 million cells and takes well under a second. A 111 m by 47 m
# floor at 31 mm is 5.9 million and takes about half a minute, which in the
# browser is three. The cap keeps a pathological drawing from asking for
# gigabytes, by coarsening instead.
#
# It was 40 million, which is not a cap in any useful sense: a drawing whose
# unit came out wrong asked for a grid that size, and the build did not fail,
# it ran for twenty minutes and had to be killed. The browser has no way to
# kill it. Twice the largest real floor measured here is the most that can
# still finish, so that is what this is.
MAX_CELLS = 12_000_000


class Grid:
    """A binary raster covering a world-space bounding box.

    `bbox` is (min_x, min_y, max_x, max_y) in metres. `cell` is the size of
    one cell in metres. `mask` is a boolean array indexed [row, col], where
    row runs with +y and col runs with +x.
    """

    __slots__ = ("x0", "y0", "cell", "cols", "rows", "mask", "asked_cell")

    def __init__(self, bbox, cell, pad_cells=4):
        x0, y0, x1, y1 = bbox
        width = max(x1 - x0, cell)
        height = max(y1 - y0, cell)

        # Coarsen rather than fail if the request is absurd. `asked_cell`
        # keeps what was wanted, so a caller can say the plan came out
        # coarser than the drawing rather than leave it to be noticed.
        self.asked_cell = cell
        wanted = (width / cell + 2 * pad_cells) * (height / cell + 2 * pad_cells)
        if wanted > MAX_CELLS:
            cell *= float(np.sqrt(wanted / MAX_CELLS))

        self.cell = cell
        self.x0 = x0 - pad_cells * cell
        self.y0 = y0 - pad_cells * cell
        self.cols = int(np.ceil(width / cell)) + 2 * pad_cells
        self.rows = int(np.ceil(height / cell)) + 2 * pad_cells
        self.mask = np.zeros((self.rows, self.cols), dtype=bool)

    @classmethod
    def like(cls, other):
        """Return an empty grid with exactly `other`'s geometry.

        Masks from two grids are combined with bitwise operators, so their
        shapes have to match to the cell. Rebuilding a grid from another's
        bounding box does not guarantee that: the constructor rounds the
        width up to a whole cell, which can add a column.
        """
        grid = cls.__new__(cls)
        grid.x0, grid.y0, grid.cell = other.x0, other.y0, other.cell
        grid.asked_cell = other.asked_cell
        grid.cols, grid.rows = other.cols, other.rows
        grid.mask = np.zeros((other.rows, other.cols), dtype=bool)
        return grid

    def to_cell(self, xs, ys):
        """Convert world coordinates to (col, row) integer arrays.

        Indices are `np.intp`, the platform's own index type, not a fixed
        int64. Pyodide runs on wasm32 where intp is 32-bit, and numpy
        refuses to cast an int64 index array down to it, so an int64 here
        works on the desktop and raises in the browser.
        """
        cols = np.floor((np.asarray(xs, dtype=float) - self.x0) / self.cell)
        rows = np.floor((np.asarray(ys, dtype=float) - self.y0) / self.cell)
        return cols.astype(np.intp), rows.astype(np.intp)

    def to_world(self, cols, rows):
        """Convert cell corner indices back to world coordinates."""
        return (np.asarray(cols, dtype=float) * self.cell + self.x0,
                np.asarray(rows, dtype=float) * self.cell + self.y0)

    def set_cells(self, cols, rows):
        """Mark cells, ignoring anything outside the grid."""
        ok = ((cols >= 0) & (cols < self.cols) & (rows >= 0) & (rows < self.rows))
        if ok.any():
            self.mask[rows[ok], cols[ok]] = True


# ─────────────────────────────────────────────────────────────────────────────
# Rasterising
# ─────────────────────────────────────────────────────────────────────────────

def rasterise(grid, paths, closed_fill=False):
    """Draw `paths` into `grid`.

    Each path is a list of (x, y) points in metres. Every segment is sampled
    at half-cell spacing, which is dense enough that no cell along a segment
    is skipped. Sampling beats a per-segment Bresenham loop here because it
    vectorises: one numpy call handles every segment of every path at once.

    Set `closed_fill` to also fill each closed path's interior. Use it for
    wall bands and hatches, which are solid, not outlines.
    """
    xs_all, ys_all = [], []

    for path in paths:
        if len(path) < 2:
            if len(path) == 1:
                xs_all.append(np.array([path[0][0]]))
                ys_all.append(np.array([path[0][1]]))
            continue
        pts = np.asarray(path, dtype=float)
        starts, ends = pts[:-1], pts[1:]
        deltas = ends - starts
        lengths = np.hypot(deltas[:, 0], deltas[:, 1])
        steps = np.maximum(1, np.ceil(lengths / (grid.cell * 0.5)).astype(np.intp))

        # Build one flat parameter array covering every segment at once.
        total = int(steps.sum()) + len(steps)
        if total <= 0:
            continue
        seg_index = np.repeat(np.arange(len(steps)), steps + 1)
        within = np.arange(total) - np.repeat(
            np.cumsum(np.concatenate(([0], steps[:-1] + 1))), steps + 1)
        t = within / steps[seg_index]
        xs_all.append(starts[seg_index, 0] + deltas[seg_index, 0] * t)
        ys_all.append(starts[seg_index, 1] + deltas[seg_index, 1] * t)

    if xs_all:
        cols, rows = grid.to_cell(np.concatenate(xs_all), np.concatenate(ys_all))
        grid.set_cells(cols, rows)

    if closed_fill:
        for path in paths:
            if len(path) >= 4:
                _fill_ring(grid, path)
    return grid


def _fill_ring(grid, ring):
    """Fill one closed ring's interior by scanline crossing tests."""
    pts = np.asarray(ring, dtype=float)
    if len(pts) < 4:
        return
    if not np.allclose(pts[0], pts[-1]):
        pts = np.vstack([pts, pts[0]])

    cols, rows = grid.to_cell(pts[:, 0], pts[:, 1])
    row_lo = max(0, int(rows.min()))
    row_hi = min(grid.rows - 1, int(rows.max()))
    if row_hi < row_lo:
        return

    x0s, y0s = pts[:-1, 0], pts[:-1, 1]
    x1s, y1s = pts[1:, 0], pts[1:, 1]

    for row in range(row_lo, row_hi + 1):
        y = grid.y0 + (row + 0.5) * grid.cell
        # Half-open test on y avoids double-counting a shared vertex.
        hits = ((y0s <= y) & (y1s > y)) | ((y1s <= y) & (y0s > y))
        if not hits.any():
            continue
        dy = y1s[hits] - y0s[hits]
        xs = x0s[hits] + (y - y0s[hits]) * (x1s[hits] - x0s[hits]) / dy
        xs.sort()
        for i in range(0, len(xs) - 1, 2):
            c0 = int(np.floor((xs[i] - grid.x0) / grid.cell))
            c1 = int(np.floor((xs[i + 1] - grid.x0) / grid.cell))
            c0 = max(0, c0)
            c1 = min(grid.cols - 1, c1)
            if c1 >= c0:
                grid.mask[row, c0:c1 + 1] = True


# ─────────────────────────────────────────────────────────────────────────────
# Morphology
# ─────────────────────────────────────────────────────────────────────────────

def _shift(mask, offset, axis, fill=False):
    """Shift `mask` along `axis`, filling the vacated edge with `fill`.

    `np.roll` cannot be used here. It wraps, which lets the left and right
    walls of a plan fuse through the array boundary. An explicit fill also
    lets a caller state what lies outside the array: empty space for a
    dilation, occupied space for the complement inside an erosion.
    """
    if offset == 0:
        return mask
    out = np.full_like(mask, fill)
    if axis == 0:
        if offset > 0:
            out[offset:, :] = mask[:-offset, :]
        else:
            out[:offset, :] = mask[-offset:, :]
    else:
        if offset > 0:
            out[:, offset:] = mask[:, :-offset]
        else:
            out[:, :offset] = mask[:, -offset:]
    return out


def _dilate(mask, radius, fill=False):
    """Grow `mask` by `radius` cells, using a square structuring element.

    A square element is separable, so this runs as a pass along each axis
    instead of a 2D convolution.
    """
    if radius <= 0:
        return mask
    out = mask
    for axis in (0, 1):
        acc = out.copy()
        for shift in range(1, radius + 1):
            acc |= _shift(out, shift, axis, fill)
            acc |= _shift(out, -shift, axis, fill)
        out = acc
    return out


def _erode(mask, radius):
    """Shrink `mask` by `radius` cells. The dual of `_dilate`.

    Anything outside the array counts as empty, so a region touching the
    array edge does erode inward from it. `close_mask` and `open_mask` add
    their own margin first, which is what keeps that from biting.
    """
    if radius <= 0:
        return mask
    return ~_dilate(~mask, radius, fill=True)


def _with_margin(mask, margin, operation):
    """Run `operation` on `mask` surrounded by `margin` empty cells.

    Morphology at an array edge is only well defined once there is room for
    the structuring element to sit outside the content. Adding the margin
    here means neither the caller nor the grid has to reserve it, so a close
    always returns the shape at its original size.
    """
    if margin <= 0:
        return operation(mask)
    rows, cols = mask.shape
    padded = np.zeros((rows + 2 * margin, cols + 2 * margin), dtype=bool)
    padded[margin:-margin, margin:-margin] = mask
    result = operation(padded)
    return result[margin:-margin, margin:-margin]


def close_mask(mask, radius):
    """Close gaps up to `radius` cells wide.

    Dilating then eroding fuses a wall's two drawn faces into one solid band
    and seals the small breaks a drawing leaves at corners and door jambs,
    while leaving the wall's outer dimensions unchanged.
    """
    if radius <= 0:
        return mask
    return _with_margin(mask, radius + 1,
                        lambda m: _erode(_dilate(m, radius), radius))


def open_mask(mask, radius):
    """Remove features thinner than `radius` cells. Clears speckle."""
    if radius <= 0:
        return mask
    return _with_margin(mask, radius + 1,
                        lambda m: _dilate(_erode(m, radius), radius))


def dilate(mask, radius):
    """Grow `mask` by `radius` cells."""
    if radius <= 0:
        return mask
    return _with_margin(mask, radius + 1, lambda m: _dilate(m, radius))


def erode(mask, radius):
    """Shrink `mask` by `radius` cells, treating outside the array as empty."""
    if radius <= 0:
        return mask
    return _with_margin(mask, radius + 1, lambda m: _erode(m, radius))


def largest_component(mask, connectivity=4):
    """Return only the biggest connected region of `mask`."""
    labels, sizes = label_regions(mask, min_cells=1)
    if not sizes:
        return mask
    biggest = max(sizes, key=lambda label: sizes[label])
    return labels == biggest


def fill_holes(mask):
    """Fill every enclosed empty region, leaving only the outer boundary.

    A floor footprint has rooms and voids punched through it. Filling them
    turns the footprint into one solid slab, which is what the building's
    outer boundary is traced from.
    """
    outside = ~mask
    labels, sizes = label_regions(outside, min_cells=1)
    if not sizes:
        return mask
    border = set(labels[0, :].tolist()) | set(labels[-1, :].tolist()) \
        | set(labels[:, 0].tolist()) | set(labels[:, -1].tolist())
    border.discard(0)
    if not border:
        return np.ones_like(mask)
    reachable = np.isin(labels, list(border))
    return ~reachable


def boundary_band(mask, radius):
    """Return the inward band of `radius` cells along a mask's own edge.

    Used to make a perimeter wall from a floor outline. A drawing whose
    facade is a glazed curtain wall has no perimeter wall line to trace, only
    mullion marks, so the envelope has to be constructed from the footprint.
    """
    if radius <= 0:
        return np.zeros_like(mask)
    return mask & ~erode(mask, radius)


def thicken_thin(mask, radius, core_radius=2):
    """Grow only the parts of `mask` that are thinner than a real wall.

    Drawings represent a wall two ways. Some draw both faces, which closing
    fuses into a band of the right thickness. Others draw one line down the
    middle, and no amount of closing can recover a thickness that was never
    drawn: the line has to be given one.

    Both appear in the same file, so the two cases have to be told apart
    rather than chosen between. A band survives a small erosion and keeps a
    core; a single line does not. Only what has no core is grown, which
    leaves correctly drawn walls at their true thickness.
    """
    if radius <= 0:
        return mask

    def run(m):
        core = _erode(m, core_radius)
        thin = m & ~_dilate(core, core_radius)
        if not thin.any():
            return m
        return m | _dilate(thin, radius)

    return _with_margin(mask, radius + core_radius + 1, run)


# ─────────────────────────────────────────────────────────────────────────────
# Connected components
# ─────────────────────────────────────────────────────────────────────────────

def label_regions(mask, min_cells=1):
    """Label 4-connected True regions in `mask`.

    Returns (labels, sizes) where `labels` is an int array with 0 for
    background and 1..n for regions, and `sizes` maps a label to its cell
    count. Regions smaller than `min_cells` are dropped.

    Works on row runs rather than individual cells. A floor plan raster has
    millions of cells but only thousands of runs, so this stays fast without
    scipy.
    """
    rows, cols = mask.shape
    labels = np.zeros((rows, cols), dtype=np.int32)

    # Find horizontal runs of True, per row, vectorised.
    padded = np.zeros((rows, cols + 2), dtype=bool)
    padded[:, 1:-1] = mask
    diff = np.diff(padded.astype(np.int8), axis=1)
    run_rows, run_starts = np.nonzero(diff == 1)
    _, run_ends = np.nonzero(diff == -1)     # exclusive end

    if len(run_starts) == 0:
        return labels, {}

    parent = list(range(len(run_starts) + 1))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    # Index runs by row so each row only compares against the one above it.
    runs_by_row = {}
    for idx in range(len(run_starts)):
        runs_by_row.setdefault(int(run_rows[idx]), []).append(idx)

    for row in sorted(runs_by_row):
        above = runs_by_row.get(row - 1)
        if not above:
            continue
        here = runs_by_row[row]
        j = 0
        for idx in here:
            s0, e0 = run_starts[idx], run_ends[idx]
            # Runs are sorted by start within a row, so walk both lists once.
            while j < len(above) and run_ends[above[j]] <= s0:
                j += 1
            k = j
            while k < len(above) and run_starts[above[k]] < e0:
                union(idx, above[k])
                k += 1

    # Compact the root ids into 1..n and measure each region.
    remap = {}
    sizes = {}
    for idx in range(len(run_starts)):
        root = find(idx)
        label = remap.get(root)
        if label is None:
            label = len(remap) + 1
            remap[root] = label
        sizes[label] = sizes.get(label, 0) + int(run_ends[idx] - run_starts[idx])

    keep = {label for label, n in sizes.items() if n >= min_cells}
    renumber = {}
    for label in sorted(keep):
        renumber[label] = len(renumber) + 1

    for idx in range(len(run_starts)):
        label = renumber.get(remap[find(idx)], 0)
        if label:
            labels[run_rows[idx], run_starts[idx]:run_ends[idx]] = label

    final_sizes = {}
    for label, n in sizes.items():
        new = renumber.get(label)
        if new:
            final_sizes[new] = n
    return labels, final_sizes


def nearest_seed_labels(mask, seeds):
    """Split one connected region of `mask` by which seed is nearest.

    `seeds` is a list of (row, col, label) with label >= 1. Every True cell
    in `mask` is given the label of whichever seed reaches it first,
    growing every seed's frontier outward one ring at a time -- a plain
    4-connected multi-source flood fill, which is exact grid-step distance
    since every step costs the same. A cell with no path to any seed (cut
    off in its own pocket of the mask) keeps label 0.

    A room whose partitions do not fully enclose it is still, in every real
    building, closer to its own label than to its neighbour's: labels sit at
    room spacing, and the gap is only ever one missing wall, not a missing
    floor. This is what turns one leaked region back into its rooms without
    needing the wall that failed to close.

    One Python-level BFS step per cell would mean millions of interpreter
    iterations on a real leaked region. Growing every frontier by a ring at
    once instead makes each round four vectorised numpy shifts over the
    whole array, so the cost is one round per cell of the widest gap
    between a seed and the room it names, not one round per cell of the
    region.
    """
    owner = np.zeros(mask.shape, dtype=np.int32)
    for r, c, label in seeds:
        if 0 <= r < mask.shape[0] and 0 <= c < mask.shape[1] and mask[r, c]:
            owner[r, c] = label

    if not owner.any():
        return owner

    unclaimed = mask & (owner == 0)
    while unclaimed.any():
        grown = np.zeros_like(owner)
        for axis in (0, 1):
            for step in (1, -1):
                shifted = _shift(owner, step, axis, fill=0)
                take = unclaimed & (grown == 0) & (shifted != 0)
                grown[take] = shifted[take]
        if not grown.any():
            break
        owner = np.where(grown != 0, grown, owner)
        unclaimed &= grown == 0
    return owner


# ─────────────────────────────────────────────────────────────────────────────
# Contour tracing
# ─────────────────────────────────────────────────────────────────────────────

def trace_polygons(mask, grid, simplify=1.0, min_area_cells=4):
    """Trace `mask` into polygons, in world coordinates.

    Returns a list of polygons, each a list of rings, each ring a list of
    [x, y] points closed at both ends. The first ring is the exterior and
    any others are holes, matching GeoJSON's polygon layout.

    The boundary of a cell region is a set of unit edges on the cell grid.
    Chaining those edges gives exactly rectilinear outlines, which suits
    architecture: walls really are axis-aligned, so the traced result is
    faithful rather than a stair-stepped approximation of a curve.
    """
    labels, sizes = label_regions(mask, min_cells=max(1, min_area_cells))
    if not sizes:
        return []

    polygons = []
    for label in sorted(sizes):
        rings = _trace_label(labels == label)
        if not rings:
            continue
        world_rings = []
        for ring, signed in rings:
            pts = _simplify_ring(ring, simplify)
            if len(pts) < 4:
                continue
            xs, ys = grid.to_world([p[0] for p in pts], [p[1] for p in pts])
            world_rings.append((list(map(list, zip(xs.tolist(), ys.tolist()))), signed))
        if not world_rings:
            continue

        # Largest absolute area is the exterior; the rest are holes.
        world_rings.sort(key=lambda r: -abs(_ring_area(r[0])))
        exterior = _orient(world_rings[0][0], positive=True)
        holes = [_orient(r[0], positive=False) for r in world_rings[1:]]
        polygons.append([exterior] + holes)
    return polygons


def _trace_label(region):
    """Return every boundary ring of one labelled region, in cell space.

    Each ring comes back as (points, signed_area). Points are cell-corner
    indices, so they are exact integers and chain without tolerance.
    """
    rows, cols = region.shape
    padded = np.zeros((rows + 2, cols + 2), dtype=bool)
    padded[1:-1, 1:-1] = region

    # A filled cell contributes a boundary edge on each side whose neighbour
    # is empty. Directions are chosen so that filled area stays on the left,
    # which makes exteriors wind one way and holes the other.
    filled = padded
    up = filled & ~np.roll(filled, -1, axis=1)     # right neighbour empty
    down = filled & ~np.roll(filled, 1, axis=1)    # left neighbour empty
    left = filled & ~np.roll(filled, -1, axis=0)   # top neighbour empty
    right = filled & ~np.roll(filled, 1, axis=0)   # bottom neighbour empty

    edges = {}

    def add(kind, array):
        rs, cs = np.nonzero(array)
        # Padded cell (r, c) is region cell (r-1, c-1), whose corners in the
        # grid's own corner space run from (c-1, r-1) to (c, r).
        for r, c in zip((rs - 1).tolist(), (cs - 1).tolist()):
            if kind == "up":
                a, b = (c + 1, r), (c + 1, r + 1)
            elif kind == "left":
                a, b = (c + 1, r + 1), (c, r + 1)
            elif kind == "down":
                a, b = (c, r + 1), (c, r)
            else:  # right
                a, b = (c, r), (c + 1, r)
            edges.setdefault(a, []).append(b)

    add("up", up)
    add("left", left)
    add("down", down)
    add("right", right)

    rings = []
    while edges:
        start = next(iter(edges))
        ring = [start]
        current = start
        while True:
            options = edges.get(current)
            if not options:
                break
            nxt = options.pop()
            if not options:
                del edges[current]
            ring.append(nxt)
            current = nxt
            if current == start:
                break
        if len(ring) >= 4:
            rings.append((ring, _ring_area(ring)))
    return rings


def _ring_area(ring):
    """Signed area of a ring. Positive is counter-clockwise."""
    total = 0.0
    for i in range(len(ring) - 1):
        total += ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1]
    return total / 2.0


def _orient(ring, positive=True):
    """Force a ring's winding. GeoJSON wants exteriors CCW and holes CW."""
    if (_ring_area(ring) >= 0) != positive:
        return ring[::-1]
    return ring


def _simplify_ring(ring, tolerance):
    """Drop collinear vertices, then apply Douglas-Peucker.

    Collinear removal alone takes a traced rectilinear outline from thousands
    of unit steps down to its real corners, so it runs first and does most of
    the work at no cost in accuracy.
    """
    pts = _drop_collinear(ring)
    if tolerance > 0 and len(pts) > 4:
        closed = pts[0] == pts[-1]
        core = pts[:-1] if closed else pts
        kept = _douglas_peucker(core, tolerance)
        if len(kept) >= 3:
            pts = kept + [kept[0]] if closed else kept
    return _drop_collinear(pts)


def _drop_collinear(pts, eps=1e-9):
    """Remove points that lie on the straight line between their neighbours."""
    if len(pts) < 3:
        return list(pts)
    closed = pts[0] == pts[-1]
    core = list(pts[:-1]) if closed else list(pts)
    n = len(core)
    if n < 3:
        return list(pts)
    keep = []
    for i in range(n):
        prev = core[i - 1]
        cur = core[i]
        nxt = core[(i + 1) % n]
        cross = ((cur[0] - prev[0]) * (nxt[1] - prev[1])
                 - (cur[1] - prev[1]) * (nxt[0] - prev[0]))
        if abs(cross) > eps:
            keep.append(cur)
    if len(keep) < 3:
        return list(pts)
    return keep + [keep[0]] if closed else keep


def _douglas_peucker(pts, tolerance):
    """Classic polyline simplification, iterative to avoid deep recursion."""
    if len(pts) < 3:
        return list(pts)
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        lo, hi = stack.pop()
        if hi <= lo + 1:
            continue
        ax, ay = pts[lo]
        bx, by = pts[hi]
        dx, dy = bx - ax, by - ay
        norm = dx * dx + dy * dy
        worst, worst_i = -1.0, -1
        for i in range(lo + 1, hi):
            px, py = pts[i]
            if norm <= 0:
                dist = (px - ax) ** 2 + (py - ay) ** 2
            else:
                t = ((px - ax) * dx + (py - ay) * dy) / norm
                t = 0.0 if t < 0 else (1.0 if t > 1 else t)
                dist = (px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2
            if dist > worst:
                worst, worst_i = dist, i
        if worst > tolerance * tolerance and worst_i > 0:
            keep[worst_i] = True
            stack.append((lo, worst_i))
            stack.append((worst_i, hi))
    return [p for p, k in zip(pts, keep) if k]
