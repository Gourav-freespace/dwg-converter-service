"""
Freespace element taxonomy and classification.
==============================================

Maps DXF block names, layer names and space labels onto the element taxonomy
used by the Freespace GeoJSON format:

    elementType : Space | Furniture | Structure | Decor
    class       : a broad group, such as Work or Chair
    subClass    : the specific type, such as Focus Room or Task Chair

The vocabulary follows the reference export exactly wherever a term for
something already exists there. Terms are added only for things the reference
has no word for, such as an elevator or a restroom. `EXTENSIONS` lists every
added term, so the difference from the reference stays auditable.

Classification is name-driven first and size-driven second. A DXF exported
from Revit carries real catalogue names, and the name is a far stronger
signal than a footprint. `Chair - Naughtone Viv Barstool` is a bar stool
whatever its bounding box says. Size only decides cases the name leaves open.
"""

from __future__ import annotations

import re

# ─────────────────────────────────────────────────────────────────────────────
# Vocabulary
# ─────────────────────────────────────────────────────────────────────────────

# Terms present in the reference export. Anything classified into one of
# these is guaranteed to round-trip into a consumer built for that file.
REFERENCE_TERMS = {
    ("Space", "Work", "Open Workspace"),
    ("Space", "Work", "Enclosed Workspace"),
    ("Space", "Work", "Focus Room"),
    ("Space", "Meet", "Meeting Room"),
    ("Space", "Circulate", "Corridor"),
    ("Furniture", "Table", "Desk"),
    ("Furniture", "Table", "Conference Table"),
    ("Furniture", "Table", "Dining Table"),
    ("Furniture", "Table", "High Table"),
    ("Furniture", "Table", "Coffee Table"),
    ("Furniture", "Chair", "Task Chair"),
    ("Furniture", "Chair", "Conference Chair"),
    ("Furniture", "Chair", "Bar Stool"),
    ("Furniture", "Sofa", "Sofa"),
    ("Furniture", "Sofa", "Armchair"),
    ("Furniture", "Sofa", "Lounge Chair"),
    ("Furniture", "Sofa", "Ottoman"),
    ("Furniture", "Storage", "Storage Combination"),
    ("Structure", "Wall", None),
    ("Structure", "Door", None),
    ("Structure", "Window", None),
    ("Structure", "Opening", None),
    ("Decor", "Lamp", None),
}

# Terms added beyond the reference, grouped by why they were needed. Every
# one names something the reference file simply does not contain.
EXTENSIONS = {
    "Space": [
        ("Work", "Workpoint"), ("Work", "Private Office"), ("Work", "Touchdown"),
        ("Work", "Flex Room"), ("Work", "Community Table"),
        ("Meet", "Conference Room"), ("Meet", "Phone Room"),
        ("Meet", "Booth"), ("Meet", "Huddle Room"), ("Meet", "Project Bay"),
        ("Circulate", "Elevator"), ("Circulate", "Elevator Lobby"),
        ("Circulate", "Stairs"), ("Circulate", "Lobby"),
        ("Amenity", "Restroom"), ("Amenity", "Social Lounge"),
        ("Amenity", "Soft Seating"), ("Amenity", "Parents Room"),
        ("Amenity", "Mindfulness"), ("Amenity", "Kitchen"),
        ("Support", "Storage"), ("Support", "Copy Print"),
        ("Support", "Janitorial"), ("Support", "IDF"),
        ("Support", "Mechanical"), ("Support", "Electrical"),
        ("Support", "Trash Recycling"), ("Support", "Core"),
        ("Support", "Riser"), ("Support", "Lockers"),
    ],
    "Furniture": [
        ("Table", "Side Table"), ("Table", "Cafe Table"),
        ("Table", "Booth Table"), ("Table", "Training Table"),
        ("Table", "Console Table"), ("Table", "Project Table"),
        ("Table", "Laptop Table"), ("Table", "Counter"),
        ("Chair", "Stacking Chair"), ("Chair", "Sled Chair"),
        ("Sofa", "Bench"), ("Sofa", "Modular Seating"),
        ("Table", "Desk Beam"),
        ("Storage", "Credenza"), ("Storage", "Cabinet"),
        ("Storage", "Shelving"), ("Storage", "Locker"), ("Storage", "Cart"),
        ("Equipment", "Display"), ("Equipment", "Monitor Arm"),
        ("Equipment", "AV Stand"), ("Equipment", "Copier"),
        ("Equipment", "Refrigerator"), ("Equipment", "Coffee Machine"),
        ("Equipment", "Vending Machine"), ("Equipment", "Pod"),
        ("Equipment", "Whiteboard"), ("Equipment", "Screen"),
        ("Equipment", "Sink"), ("Equipment", "Recycling"),
        ("Other", None),
    ],
    "Structure": [
        ("Column", None), ("Partition", None), ("Window", "Mullion"),
        ("Railing", None), ("Stair", None), ("Other", None),
    ],
    "Decor": [
        ("Plant", None), ("Artwork", None), ("Rug", None),
    ],
}

# Seats a piece of furniture provides. The reference records `capacity` on
# seating only, never on a table or a storage unit, so anything absent here
# gets no capacity field at all.
SEATING_CAPACITY = {
    "Task Chair": 1, "Conference Chair": 1, "Bar Stool": 1,
    "Stacking Chair": 1, "Sled Chair": 1,
    "Armchair": 1, "Lounge Chair": 1, "Ottoman": 1,
    "Sofa": 2, "Bench": 2, "Modular Seating": 1,
}

# These are the reference file's own values, and there are only three
# combinations in the whole of it: one for furniture and decor, one for
# spaces and most structure, and one for the wall mass. That flatness is
# deliberate on Freespace's part. The colours in a Freespace floor plan --
# green desks, blue meeting tables, grey circulation -- are worked out by
# the viewer from `fsAttributes`, not read out of the file. So these stay
# exactly as the reference has them, and the plan view gets its own palette
# in `RENDER` below.
STYLE = {
    "Space":     {"fill": "#ffffff", "stroke": "#929baa"},
    "Furniture": {"fill": "#f1f2f3", "stroke": "#929baa"},
    "Decor":     {"fill": "#f1f2f3", "stroke": "#929baa"},
    "Structure": {"fill": "#ffffff", "stroke": "#929baa"},
}
# The wall mass is the one element the reference draws solid and dark.
WALL_STYLE = {"fill": "#4d586b", "stroke": "#4d586b"}


def style_for(element_type, cls):
    """Return the fill and stroke the reference file carries. Export only.

    For the plan view use `render_style_for`, which is where the colours
    that make a floor plan readable live.
    """
    if cls == "Wall":
        return dict(WALL_STYLE)
    return dict(STYLE.get(element_type, STYLE["Furniture"]))


# ─────────────────────────────────────────────────────────────────────────────
# Plan-view palette
# ─────────────────────────────────────────────────────────────────────────────
#
# This is what a Freespace floor plan looks like, and it is keyed on the
# classification rather than the element type, because here the colour is
# what carries the meaning. Read down the table to read the plan: a desk is
# green, a meeting table is blue, a seat is a solid dot in its room's
# colour, and everything else is the same neutral grey.
#
# The white strokes matter. They are what separate the desks in a bench run
# from each other, so six desks read as six desks and not as one green slab.

FLOOR_PLATE = {"fill": "#edeff1", "stroke": "#e4e7ea"}

WORK_FILL, WORK_SEAT = "#d5eed5", "#4caf50"
MEET_FILL, MEET_SEAT = "#d5ddf7", "#93a9e8"
NEUTRAL = {"fill": "#e9ebee", "stroke": "#d3d7dc"}

RENDER = {
    "Space":     {"fill": "#ffffff", "stroke": "#ffffff"},
    "Furniture": dict(NEUTRAL),
    "Decor":     dict(NEUTRAL),
    "Structure": {"fill": "#8b96a4", "stroke": "#7d8896"},
}

RENDER_BY_CLASS = {
    ("Structure", "Wall"):      {"fill": "#8b96a4", "stroke": "#7d8896"},
    ("Structure", "Partition"): {"fill": "#8b96a4", "stroke": "#7d8896"},
    # A column reads darker, which is how a plan tells structure from
    # partition at a glance.
    ("Structure", "Column"):    {"fill": "#6f7b8a", "stroke": "#6f7b8a"},
    # Glazing belongs to the wall band, so it takes the wall's own fill with
    # a white edge. Given a colour of its own it reads as a row of blue
    # panels stuck to the outside of the building.
    ("Structure", "Window"):    {"fill": "#8b96a4", "stroke": "#ffffff"},
    ("Structure", "Door"):      {"fill": None,      "stroke": "#b9c0c9"},
    ("Structure", "Opening"):   {"fill": "#ffffff", "stroke": "#ffffff"},
    ("Structure", "Stair"):     {"fill": "#e3e6ea", "stroke": "#9aa4b1"},
    ("Structure", "Railing"):   {"fill": None,      "stroke": "#9aa4b1"},
    # Circulation takes no fill, so the floor plate shows through it. That
    # is what makes a corridor read as a corridor and not as a room.
    ("Space", "Circulate"):     {"fill": None,      "stroke": None},
}

RENDER_BY_SUB_CLASS = {
    ("Furniture", "Table", "Coffee Table"): dict(NEUTRAL),
    ("Furniture", "Table", "Side Table"):   dict(NEUTRAL),
    # A Serraview overlay draws one polygon per desk allocation. Colouring
    # those green matches the plan even on a drawing that carries the
    # overlay but no furniture blocks at all.
    ("Space", "Work", "Workpoint"):
        {"fill": WORK_FILL, "stroke": "#ffffff"},
}

# A seat takes the colour of the room it stands in: green at a desk, blue in
# a meeting room, neutral anywhere else. That last part is what stops every
# cushion in a lounge from reading as a bookable seat.
SEAT_BY_SPACE = {"Work": WORK_SEAT, "Meet": MEET_SEAT}
# A seat is drawn as a marker at its centre rather than as its traced
# outline. A CAD chair block is a circle with a bite out of it for the arms,
# and at plan scale a row of those reads as debris; a plan draws a seat as a
# dot. The real outline is still what the model and the export carry, and
# still what the canvas hit-tests against.
SEAT_SHAPE = {"Work": "circle", "Meet": "square"}
# A table's colour comes from its sub-class, which `refine_by_space` has
# already corrected using the room it stands in. Reading the room again here
# would apply the same evidence twice, and would leave a conference table
# grey whenever it sits in a room nobody labelled a meeting room.
TABLE_FILL = {
    "Desk": WORK_FILL,
    "Desk Beam": WORK_FILL,
    "Workpoint": WORK_FILL,
    "Conference Table": MEET_FILL,
    "Training Table": MEET_FILL,
    "Project Table": MEET_FILL,
    "Booth Table": MEET_FILL,
    "Community Table": MEET_FILL,
}
# Only for a table whose sub-class says nothing either way.
TABLE_FILL_BY_SPACE = {"Work": WORK_FILL, "Meet": MEET_FILL}


def render_style_for(element_type, cls, sub_class=None, space_cls=None):
    """Return the plan view's fill and stroke for one classified element.

    `space_cls` is the class of the space the element stands in, when that
    is known. It is what separates a green desk seat from a blue meeting
    seat: the two are the same object, and only the room tells them apart.
    """
    if element_type == "Furniture" and cls == "Chair":
        colour = SEAT_BY_SPACE.get(space_cls)
        if not colour:
            return dict(NEUTRAL)
        return {"fill": colour, "stroke": colour,
                "seat": SEAT_SHAPE.get(space_cls, "circle")}

    if element_type == "Furniture" and cls == "Table":
        fill = TABLE_FILL.get(sub_class) \
            or (TABLE_FILL_BY_SPACE.get(space_cls) if sub_class is None else None)
        if fill:
            return {"fill": fill, "stroke": "#ffffff"}

    found = RENDER_BY_SUB_CLASS.get((element_type, cls, sub_class)) \
        or RENDER_BY_CLASS.get((element_type, cls))
    if found:
        return dict(found)
    return dict(RENDER.get(element_type, RENDER["Furniture"]))


# ─────────────────────────────────────────────────────────────────────────────
# Block-name rules
# ─────────────────────────────────────────────────────────────────────────────
#
# Ordered most specific first, and the first match wins. Order matters a lot:
# "Conference Table" must be tested before the bare "table", and
# "Bar Stool" before "stool", or the general rule swallows the specific one.

_BLOCK_RULES = [
    # ---- Structure -----------------------------------------------------
    # The reference calls facade glazing Structure/Window, so that class is
    # kept and the specific part is named in the subClass instead. A mullion
    # is part of a window wall, not a separate kind of thing.
    (r"mullion|\btransom\b", ("Structure", "Window", "Mullion")),
    (r"system\s*panel|glazed\s*partition|partition\s*panel|\bscreen\s*wall\b",
     ("Structure", "Partition", None)),
    (r"handrail|\brailing\b|\bbalustrade\b", ("Structure", "Railing", None)),
    (r"\bcolumn\b|\bcol[-_]?hr\b|\bpillar\b|\bpier\b", ("Structure", "Column", None)),
    (r"\bcladding\b|\bcore[-_ ]?hr\b", ("Structure", "Partition", None)),
    (r"\bstair(case|well)?\b|\bstep\b|\btread\b", ("Structure", "Stair", None)),
    # "IntSgl" and "IntDbl" carry no word boundary before the abbreviation, so
    # these are matched without one. Revit door families are named this way.
    (r"\bdoor\b|sgl\b|dbl\b|\bm\s+single\b|\bm\s+double\b|clear\s*opening|"
     r"sliding\s*door|door\s*typ|door\s*riser|\bleaf\b", ("Structure", "Door", None)),
    (r"\bwindow\b|\bglazing\b|\bcurtain\s*wall\b", ("Structure", "Window", None)),

    # ---- Equipment -----------------------------------------------------
    # Tested before furniture so a "Mobile AV Stand" is not read as a table.
    (r"framery|\bwork\s*pod\b|\bphone\s*booth\s*pod\b|acoustic\s*pod",
     ("Furniture", "Equipment", "Pod")),
    (r"monitor\s*arm|\bma-\d+|thunderbold|screen\s*arm",
     ("Furniture", "Equipment", "Monitor Arm")),
    (r"\bav\s*stand\b|\bav\s*cart\b", ("Furniture", "Equipment", "AV Stand")),
    (r"\bsink\b|\bfranke\b|\bmidas\b|\btap\b|\bbasin\b",
     ("Furniture", "Equipment", "Sink")),
    (r"recycle\s*unit|trash\s*unit|\bbin\b", ("Furniture", "Equipment", "Recycling")),
    (r"\blcd\b|\btv\b|\bmonitor\b|\bdisplay\b|\bvideo\s*wall\b",
     ("Furniture", "Equipment", "Display")),
    (r"copier|\bm_copier\b|printer|\bmfd\b", ("Furniture", "Equipment", "Copier")),
    (r"fridge|refrigerat", ("Furniture", "Equipment", "Refrigerator")),
    (r"coffee\s*machine|espresso|\bbarista\b",
     ("Furniture", "Equipment", "Coffee Machine")),
    (r"vending", ("Furniture", "Equipment", "Vending Machine")),
    (r"whiteboard|\bmarker\s*board\b", ("Furniture", "Equipment", "Whiteboard")),
    # A desk-mounted screen, not a display. Matched before the desk rules so a
    # "C Scape Screen" does not read as part of the desk itself.
    (r"desk\s*screen|privacy\s*screen|\bscreens?\b|\btack\s*board\b",
     ("Furniture", "Equipment", "Screen")),

    # ---- Decor ---------------------------------------------------------
    (r"\blamp\b|\bpendant\b|\bluminaire\b|\bfloor\s*light\b", ("Decor", "Lamp", None)),
    (r"planter|\bplant\b|\bpe-\d+|\bficus\b|\bfoliage\b", ("Decor", "Plant", None)),
    (r"\brug\b|\bcarpet\s*tile\b|\bmat\b", ("Decor", "Rug", None)),
    (r"gallery\s*frame|\bartwork\b|\bpicture\s*frame\b|\bframed\s*art\b|"
     r"culture\s*gallery", ("Decor", "Artwork", None)),
    (r"\bpeble\b|\bpebble\b|\bsculpture\b", ("Decor", "Artwork", None)),

    # ---- Chairs --------------------------------------------------------
    (r"bar\s*stool|barstool|\bstool\b|\bviv[-_ ]?bs\b",
     ("Furniture", "Chair", "Bar Stool")),
    (r"conference\s*chair|meeting\s*chair|boardroom\s*chair",
     ("Furniture", "Chair", "Conference Chair")),
    (r"stack(ing)?\s*chair|\bchair\s*stack\b", ("Furniture", "Chair", "Stacking Chair")),
    (r"sled\s*chair|\bcatifa\b", ("Furniture", "Chair", "Sled Chair")),
    (r"task\s*chair|work\s*chair|\baeron\b|\bmirra\b|\bsetu\b|\bgesture\b|"
     r"\bleap\b|\bthink\b|\bembody\b|\bch-\d+", ("Furniture", "Chair", "Task Chair")),

    # ---- Soft seating --------------------------------------------------
    (r"\bottoman\b|\bpouf\b|\bbumper\b", ("Furniture", "Sofa", "Ottoman")),
    (r"\barmchair\b|\barm\s*chair\b|\btub\s*chair\b", ("Furniture", "Sofa", "Armchair")),
    (r"lounge\s*chair|\bswivel\b|\bstay\s*lounge\b|\bredondo\b|\bhush\b",
     ("Furniture", "Sofa", "Lounge Chair")),
    (r"\bbench\b|\bbanquette\b|\bwindow\s*seat\b", ("Furniture", "Sofa", "Bench")),
    (r"\bsofa\b|\bsettee\b|\bcouch\b", ("Furniture", "Sofa", "Sofa")),
    (r"\bduplo\s*modular\b|modular\s*(seating|system|armless)|\bss-\d+|"
     r"soft\s*seat", ("Furniture", "Sofa", "Modular Seating")),

    # ---- Tables --------------------------------------------------------
    (r"conference\s*table|boardroom\s*table|\bmeeting\s*table\b|"
     r"huddle\s*(table|\d+\s*person)", ("Furniture", "Table", "Conference Table")),
    # The spine rail a bench-desk system hangs from. A real object in plan,
    # but not itself a desk, so it gets its own term.
    (r"\bbeam\b|\bdesk\s*spine\b|\bc[-\s]?scape\s*beam\b",
     ("Furniture", "Table", "Desk Beam")),
    (r"coffee\s*table", ("Furniture", "Table", "Coffee Table")),
    (r"high\s*table|poseur|standing\s*table|\bbar\s*table\b",
     ("Furniture", "Table", "High Table")),
    (r"dining\s*table", ("Furniture", "Table", "Dining Table")),
    (r"cafe\s*table|\bcanteen\s*table\b", ("Furniture", "Table", "Cafe Table")),
    (r"booth\s*table", ("Furniture", "Table", "Booth Table")),
    (r"training\s*table|flip[-\s]?top", ("Furniture", "Table", "Training Table")),
    (r"console\s*table|live\s*edge", ("Furniture", "Table", "Console Table")),
    (r"project\s*table|\bpbt-\d+|project\s*bay\s*table",
     ("Furniture", "Table", "Project Table")),
    (r"laptop\s*table", ("Furniture", "Table", "Laptop Table")),
    (r"side\s*table|\bstump\b|\bend\s*table\b", ("Furniture", "Table", "Side Table")),
    (r"counter\s*top|\bcountertop\b|\bcounter\b|\bworktop\b",
     ("Furniture", "Table", "Counter")),
    (r"\bdesk(ing)?\b|\bwk-\d+|workstation|renew\w*table|sense\s*desking|"
     r"\bbench\s*desk\b|\bratio\b", ("Furniture", "Table", "Desk")),

    # ---- Storage -------------------------------------------------------
    (r"credenza", ("Furniture", "Storage", "Credenza")),
    (r"\blocker\b|\blk-\d+|team\s*storage|\bcubb(y|ie)s?\b",
     ("Furniture", "Storage", "Locker")),
    (r"\bshel(f|ve|ves|ving)\b|\bshalves\b|\bbookcase\b",
     ("Furniture", "Storage", "Shelving")),
    (r"\bcabinet\b|\bcupboard\b|\bpedestal\b|\btambour\b|kitchenette|"
     r"\bdrawers?\b|\bpresswall\b", ("Furniture", "Storage", "Cabinet")),
    (r"\bcart\b|\btrolley\b|\bmobile\s*unit\b", ("Furniture", "Storage", "Cart")),
    (r"\bstorage\b|\bwardrobe\b|\bpresswall\b",
     ("Furniture", "Storage", "Storage Combination")),

    # ---- Weak, generic fallbacks --------------------------------------
    # Only reached when nothing above matched, so a bare "Chair" block name
    # still lands somewhere sensible rather than becoming unclassified.
    (r"\bchair\b|\bseat(ing)?\b", ("Furniture", "Chair", "Task Chair")),
    (r"\btable\b", ("Furniture", "Table", "Side Table")),
    (r"\bbed\b|\bfuton\b", ("Furniture", "Sofa", "Sofa")),
]

_BLOCK_COMPILED = [(re.compile(p, re.IGNORECASE), out) for p, out in _BLOCK_RULES]


# Revit and DWG exports bury the real name in serial numbers, level suffixes
# and view prefixes. Strip them so the rules see the catalogue name.
_NAME_NOISE = [
    re.compile(r"-LEVEL\s*\d+.*$", re.IGNORECASE),   # "-LEVEL 32 room number"
    re.compile(r"_dwg\b", re.IGNORECASE),
    re.compile(r"\b\d{6,}\b"),                        # Revit element ids
    re.compile(r"\bV\d{1,3}\b"),                      # variant suffixes
    re.compile(r"\*[UAXD]\d+", re.IGNORECASE),        # anonymous block names
    # A copy number stuck straight onto a word: lamp9, futon6, peble7,
    # screens2, Task Chair1. It has to go before matching, because a regex
    # cannot see a word boundary between a letter and a digit, so `\blamp\b`
    # never matches "lamp9" no matter how the rule is written.
    re.compile(r"(?<=[A-Za-z])\d{1,3}$"),
]


def clean_block_name(name):
    """Strip export noise from a block name, keeping the catalogue name."""
    if not name:
        return ""
    out = str(name)
    for pattern in _NAME_NOISE:
        out = pattern.sub(" ", out)
    out = re.sub(r"[_]+", " ", out)
    out = re.sub(r"\s*-\s*$", "", out)
    return re.sub(r"\s{2,}", " ", out).strip(" -")


def classify_block(name, layer="", width=None, height=None):
    """Classify a block by name, then by footprint if the name says nothing.

    Returns (element_type, cls, sub_class, confidence) where confidence is
    "name", "layer", "size" or "unknown", so a caller can report how much of
    the plan was identified for certain.
    """
    cleaned = clean_block_name(name)

    for pattern, out in _BLOCK_COMPILED:
        if pattern.search(cleaned):
            return out[0], out[1], out[2], "name"

    # Nothing in the name. Try the footprint before the layer, because a
    # measured size is real evidence and a layer only says which drawing
    # discipline owns the block.
    by_size = classify_by_size(width, height)
    if by_size:
        return by_size[0], by_size[1], by_size[2], "size"

    # Fall back to a neutral bucket for the layer's discipline. Naming an
    # unidentified architecture block "Partition" would be a guess dressed
    # up as a fact, and it is wrong far more often than it is right.
    layer_guess = classify_layer_intent(layer)
    if layer_guess == "structure":
        return "Structure", "Other", None, "layer"
    if layer_guess == "furniture":
        return "Furniture", "Other", None, "layer"
    return None, None, None, "unknown"


# ─────────────────────────────────────────────────────────────────────────────
# Size-based fallback
# ─────────────────────────────────────────────────────────────────────────────

# Footprint ranges in metres, as (min_long, max_long, min_short, max_short).
# Used only when a block carries no usable name. Ordered so the tightest
# range is tested first.
_SIZE_RULES = [
    # A chair is close to square and about half a metre across.
    (0.38, 0.75, 0.38, 0.75, ("Furniture", "Chair", "Task Chair")),
    # A desk is 1.2 m to 2.0 m long and 0.6 m to 0.9 m deep.
    (1.15, 2.05, 0.55, 0.95, ("Furniture", "Table", "Desk")),
    # A conference table is long and deep.
    (2.0, 6.0, 1.0, 2.2, ("Furniture", "Table", "Conference Table")),
    # A credenza or low storage run is long and shallow.
    (0.8, 2.4, 0.35, 0.65, ("Furniture", "Storage", "Storage Combination")),
    # A bench run: several desks sharing one spine, which a drawing with its
    # blocks exploded gives as one shape because the desks touch. It is a run
    # of desks, so call it a desk. `refine_by_space` corrects the reverse case,
    # a long table that turns out to stand in a meeting room.
    (6.0, 14.0, 1.1, 2.8, ("Furniture", "Table", "Desk")),
]

# What a table is, by the class of room it stands in. A long table is a
# conference table in a meeting room and a bench desk in a workspace, and its
# own dimensions cannot tell the two apart.
TABLE_BY_SPACE = {
    "Work": "Desk",
    "Meet": "Conference Table",
    "Amenity": "Dining Table",
}


def classify_by_size(width, height):
    """Guess a class from a footprint alone. Returns a triple or None."""
    if not width or not height or width <= 0 or height <= 0:
        return None
    long_side, short_side = max(width, height), min(width, height)
    for lo_l, hi_l, lo_s, hi_s, out in _SIZE_RULES:
        if lo_l <= long_side <= hi_l and lo_s <= short_side <= hi_s:
            return out
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Layer intent
# ─────────────────────────────────────────────────────────────────────────────

_LAYER_INTENT = [
    ("space",     r"sv-space|sv-zone|\broom\s*bound|\bspace\b|\bzone\b|"
                  r"\bgross\s*area\b|\barea-gross\b|\bgross\b"),
    # A-AREA-IDEN is the area identification, which is text and leader lines.
    # It has to read as annotation and not as a space overlay: taken as an
    # overlay its leader lines become room boundaries, and every label in
    # the drawing turns into a sliver of a space.
    ("annotation", r"sv-txt|\btext\b|\banno|\bdim\b|\bnote\b|\btag\b|\blabel\b|"
                   r"\bgrid\b|\btitle\b|\bkey\s*plan\b|\brev-|\bnplt\b|"
                   r"\barea-iden\b|\bdetl\b"),
    # Q-CASE is casework and Q-SPCQ is specialty equipment, both AIA
    # equipment-discipline layers that a fit-out drawing puts real furniture
    # on. Left unknown, twelve thousand entities of counters and cabinets
    # reach nothing at all.
    ("furniture", r"\bfurn|\bff.?e\b|\bfitout\b|\bfit.out\b|\bfixture\b|"
                  r"\bseating\b|\bdesk\b|\bchair\b|\btable\b|\bequip|"
                  r"\bcase\b|\bspcq\b"),
    # AIA layer names abbreviate, so match the short forms as well: A-COLS,
    # A-STAIRS, A-FLOR, A-RAIL. Without them a column layer reads as
    # "unknown", and its geometry never joins the wall mass.
    ("structure", r"\barch|\bwall|\bstruct|\bpartition|\bcore\b|\bcolumn\b|"
                  r"\bdoor\b|\bwindow\b|\bglaz|\bmullion\b|\bconc|\bmasonry\b|"
                  r"\bshell\b|\bfabric\b|\bcols?\b|\bcolm|\bstair|\bslab\b|"
                  r"\bflor\b|\brail|\bmull\b|\bcurtain\b|\bglass\b"),
    # SANR is the AIA short form of sanitary, and LITE of lighting. Naming
    # them keeps a toilet block and a light fitting out of the wall mass,
    # which is what "unknown" would leave open to the next rule along.
    ("service",   r"\bmech\b|\bhvac\b|\bduct\b|\belec\b|\bpower\b|\bdata\b|"
                  r"\bplumb|\bsanit|\bsanr\b|\bfire\b|\bsprink|\blite\b|"
                  r"\blight"),
]
_LAYER_COMPILED = [(k, re.compile(p, re.IGNORECASE)) for k, p in _LAYER_INTENT]


def classify_layer_intent(name):
    """Return what a layer is broadly for: space, structure, furniture, ..."""
    if not name:
        return "unknown"
    for key, pattern in _LAYER_COMPILED:
        if pattern.search(name):
            return key
    return "unknown"


# A layer's specific role, which is finer than its intent. This is the only
# evidence available in a drawing whose blocks were exploded on export: the
# geometry has no name, so the layer has to say what it is. Ordered
# most-specific-first, and doors come before walls because a door layer name
# often carries the word "wall" as well (A-WALL-DOOR).
_LAYER_ROLE = [
    (r"\bdoor\b|\bdoors\b|-dr\b",       ("Structure", "Door", None)),
    (r"\bglaz|\bwindow|\bcurtain\b|\bglass\b|\bmull",
                                        ("Structure", "Window", "Glazing")),
    (r"\bcols?\b|\bcolumn|\bcolm",      ("Structure", "Column", None)),
    (r"\bstair",                        ("Structure", "Stair", None)),
    (r"\brail|\bbalustr",               ("Structure", "Railing", None)),
    (r"\bwall|\bpartition|\bmasonry\b|\bconc|\bstruct|\bcore\b|\bslab\b",
                                        ("Structure", "Wall", None)),
    (r"\bequip|\bspcq\b",               ("Furniture", "Equipment", None)),
    (r"\bcase\b",                       ("Furniture", "Storage", "Cabinet")),
]
_LAYER_ROLE_COMPILED = [(re.compile(p, re.IGNORECASE), out)
                        for p, out in _LAYER_ROLE]


def classify_layer_role(name):
    """Return (element_type, class, sub_class) for a layer, or None.

    Finer than `classify_layer_intent`. Use it for loose geometry, where
    there is no block name to read.
    """
    if not name:
        return None
    for pattern, out in _LAYER_ROLE_COMPILED:
        if pattern.search(name):
            return out
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Space-label rules
# ─────────────────────────────────────────────────────────────────────────────
#
# Drives both the Serraview `SV-TXT-TYPE` overlay in the reference DXF and any
# plain room-name text found inside a traced room, so one table serves both.

_SPACE_RULES = [
    # ---- Work ----------------------------------------------------------
    (r"open\s*(plan|workspace|office)|\bbench\s*desk", ("Work", "Open Workspace")),
    (r"private\s*office|\bcellular\s*office\b|\bdirector'?s?\s*office\b",
     ("Work", "Private Office")),
    (r"focus\s*(room|pod|booth)|\bquiet\s*room\b", ("Work", "Focus Room")),
    (r"touchdown|\bhot\s*desk\b|\bdrop[-\s]?in\b", ("Work", "Touchdown")),
    (r"community\s*table|\bteam\s*table\b", ("Work", "Community Table")),
    (r"flex\s*room|\blibrary\b|\bliving\s*room\b", ("Work", "Flex Room")),
    # A single desk allocation, not a room. Serraview draws one polygon per
    # desk on its overlay, so these arrive in the hundreds and must not each
    # become an "Open Workspace".
    (r"desk\s*[:\-]|^desk$|\bworkstation\b|\bworkpoint\b|\bwork\s*seat\b",
     ("Work", "Workpoint")),
    (r"\boffice\b|\bwork\s*room\b", ("Work", "Enclosed Workspace")),

    # ---- Meet ----------------------------------------------------------
    (r"conference\s*room|\bboardroom\b|\bconf\b", ("Meet", "Conference Room")),
    (r"phone\s*(room|booth)|\bcall\s*(room|booth)\b", ("Meet", "Phone Room")),
    (r"huddle", ("Meet", "Huddle Room")),
    (r"project\s*bay|\bwar\s*room\b|\bscrum\b", ("Meet", "Project Bay")),
    (r"\bbooth\b", ("Meet", "Booth")),
    (r"meeting\s*room|\bmeet\b|\btraining\s*room\b|\bseminar\b",
     ("Meet", "Meeting Room")),

    # ---- Circulate -----------------------------------------------------
    (r"elevator\s*lobby|\blift\s*lobby\b", ("Circulate", "Elevator Lobby")),
    (r"\belevator\b|\blift\b", ("Circulate", "Elevator")),
    (r"\bstair|communicating\s*stair|\bfire\s*escape\b", ("Circulate", "Stairs")),
    (r"\bcorridor\b|\bcircul|\bwalkway\b|\bpassage\b|\baisle\b",
     ("Circulate", "Corridor")),
    (r"\blobby\b|\breception\b|\bfoyer\b|\bentrance\b", ("Circulate", "Lobby")),

    # ---- Amenity -------------------------------------------------------
    (r"restroom|\btoilet\b|\bwc\b|\bbathroom\b|\bshower\b|\blavatory\b",
     ("Amenity", "Restroom")),
    (r"social\s*lounge|\bbreakout\b|\bcollab\w*\s*(area|zone)\b",
     ("Amenity", "Social Lounge")),
    (r"soft\s*seat", ("Amenity", "Soft Seating")),
    (r"parent'?s?\s*room|\bmother'?s?\s*room\b|\bnursing\b|\blactation\b",
     ("Amenity", "Parents Room")),
    (r"mindful|\bwellness\b|\bprayer\b|\bmeditation\b", ("Amenity", "Mindfulness")),
    (r"\bkitchen\b|\bpantry\b|\btea\s*point\b|\bcafe\b|\bcanteen\b|"
     r"\bbreak\s*room\b", ("Amenity", "Kitchen")),

    # ---- Support -------------------------------------------------------
    (r"copy\s*/?\s*print|\bprint\s*(room|centre|center)\b|\breprograph",
     ("Support", "Copy Print")),
    (r"janitor|\bcleaner'?s?\b|\bhousekeep", ("Support", "Janitorial")),
    (r"\bidf\b|\bmdf\b|\bcomms\s*room\b|\bserver\s*room\b|\bhub\s*room\b",
     ("Support", "IDF")),
    (r"mechanical|\bplant\s*room\b|\bhvac\b|\bahu\b", ("Support", "Mechanical")),
    (r"electrical|\bswitch\s*room\b|\bups\b", ("Support", "Electrical")),
    (r"trash|\brecycl|\bwaste\b|\brefuse\b", ("Support", "Trash Recycling")),
    (r"vertical\s*duct|\bduct\b|\bvoid\b|\briser\b|\bshaft\b", ("Support", "Riser")),
    (r"\bcubb(y|ie)s?\b|\blocker", ("Support", "Lockers")),
    (r"\bcore\b", ("Support", "Core")),
    (r"\bstore\b|\bstorage\b|\bstock\b", ("Support", "Storage")),
]
_SPACE_COMPILED = [(re.compile(p, re.IGNORECASE), out) for p, out in _SPACE_RULES]


def classify_space(label, area=None):
    """Classify a space from its label text.

    Returns (cls, sub_class, confidence). When the label says nothing, falls
    back to area alone, which can only distinguish broad size bands.
    """
    text = (label or "").strip()
    if text:
        for pattern, out in _SPACE_COMPILED:
            if pattern.search(text):
                return out[0], out[1], "label"

    if area:
        # A label-free room can still be placed roughly by size. These bands
        # are deliberately coarse: guessing a specific room type from area
        # alone is not reliable, so the result stays broad.
        if area < 2.5:
            return "Support", "Storage", "area"
        if area < 5.0:
            return "Work", "Focus Room", "area"
        if area < 30.0:
            return "Work", "Enclosed Workspace", "area"
        return "Work", "Open Workspace", "area"

    return "Work", "Open Workspace", "unknown"


# Circulation and service spaces hold no seats and take no capacity, matching
# how the reference treats a corridor.
NO_CAPACITY_CLASSES = {"Circulate", "Support"}


def seats_for(sub_class):
    """Return how many people a piece of furniture seats, or None."""
    return SEATING_CAPACITY.get(sub_class)


def is_seating(sub_class):
    """True when this sub-class is something a person sits on."""
    return sub_class in SEATING_CAPACITY


# Terms renamed on the way into the exported file, and nowhere else.
#
# The pipeline keeps its own finer word because that word does work: a
# Workpoint is one desk allocation off a Serraview overlay, and it is what
# gives such a polygon a capacity of 1 and the green desk fill, where a real
# open-plan zone counts the seats standing in it and is drawn white. The
# exported file has no use for the distinction and the reference has no word
# for it, so it gets the reference's own term.
EXPORT_RENAMES = {
    ("Space", "Work", "Workpoint"): "Open Workspace",
}


def export_sub_class(element_type, cls, sub_class):
    """The sub-class to write to the file, which may not be the internal one."""
    return EXPORT_RENAMES.get((element_type, cls, sub_class), sub_class)


def all_terms(as_exported=False):
    """Return every term this module can emit, for reporting and validation.

    With `as_exported`, the terms are the ones that reach the file, which is
    what a reader of the file sees. Without it, they are the pipeline's own.
    """
    terms = set(REFERENCE_TERMS)
    for element_type, pairs in EXTENSIONS.items():
        for cls, sub in pairs:
            terms.add((element_type, cls, sub))
    if as_exported:
        terms = {(element_type, cls, export_sub_class(element_type, cls, sub))
                 for element_type, cls, sub in terms}
    return terms


def is_reference_term(element_type, cls, sub_class):
    """True when a term also appears in the reference export."""
    return (element_type, cls, sub_class) in REFERENCE_TERMS
