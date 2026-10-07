"""
Freespace GeoJSON export.
=========================

Writes a clean floor-plan model as a FeatureCollection in the same shape as
the reference export, so anything that reads one reads the other.

Coordinates
-----------
The reference file stores degrees, not metres, at 111319.4907932736 metres
per degree. That is the equatorial metres-per-degree of a sphere of radius
6378137 m, the constant Web Mercator uses. Both axes use it unchanged and
the plan sits centred on the origin, which means the file is not
georeferenced: it is a metric plan expressed in degrees.

This module matches that by default. Give `origin` a real latitude and
longitude to place the plan on the map instead, and the longitude scale is
then divided by cos(latitude) so east-west distances stay true.

Per-element shape
-----------------
Each element type carries a specific geometry and a specific set of
populated fields, and the reference is consistent about it:

    Space      Polygon           area and center set, capacity set
    Furniture  Polygon           center set, area null, capacity on seating
    Decor      Polygon           center set, area null
    Opening    Polygon           area and center null
    Door       MultiLineString   area and center null
    Window     MultiPolygon      area and center null
    Wall       MultiPolygon      one feature for the whole floor,
                                 id "wallContours", area and center null
"""

from __future__ import annotations

import json
import math
import uuid

import fs_taxonomy as tax

# Metres per degree at the equator, on a sphere of radius 6378137 m. This is
# the exact constant the reference file was written with: dividing its
# polygon areas in degrees into the areas it declares in square metres
# recovers 111319.49 to seven figures.
METRES_PER_DEGREE = 111319.4907932736

COORD_PRECISION = 12
AREA_PRECISION = 6


class Projector:
    """Converts model metres into the output's degrees."""

    def __init__(self, origin=(0.0, 0.0)):
        self.lat0, self.lon0 = float(origin[0]), float(origin[1])
        self.y_scale = METRES_PER_DEGREE
        # Away from the equator a degree of longitude is shorter. Without
        # this the plan would be stretched east to west.
        cos_lat = math.cos(math.radians(self.lat0))
        self.x_scale = METRES_PER_DEGREE * (cos_lat if abs(cos_lat) > 1e-6 else 1.0)

    def point(self, xy):
        return [round(self.lon0 + xy[0] / self.x_scale, COORD_PRECISION),
                round(self.lat0 + xy[1] / self.y_scale, COORD_PRECISION)]

    def ring(self, ring):
        return [self.point(p) for p in ring]

    def rings(self, rings):
        return [self.ring(r) for r in rings]


def _feature(geometry, element_type, cls, sub_class,
             feature_id=None, space_id=None, name=None, custom_id=None,
             area=None, center=None, capacity=None, custom=None):
    """Assemble one feature with the reference file's exact property set."""
    fs_attributes = {
        "elementType": element_type,
        "class": cls,
        # Not always the pipeline's own word for it: see
        # `fs_taxonomy.EXPORT_RENAMES`.
        "subClass": tax.export_sub_class(element_type, cls, sub_class),
    }
    if capacity is not None:
        fs_attributes["capacity"] = capacity

    style = tax.style_for(element_type, cls)
    return {
        "type": "Feature",
        "properties": {
            "id": feature_id or str(uuid.uuid4()),
            "spaceId": space_id,
            "groupId": None,
            "name": name,
            "customId": custom_id,
            "customAttributes": custom or {},
            "fsAttributes": fs_attributes,
            "area": round(area, AREA_PRECISION) if area is not None else None,
            "center": center,
            "fill": style["fill"],
            "stroke": style["stroke"],
            "fill-opacity": 1,
            "stroke-opacity": 1,
            "stroke-width": 1,
        },
        "geometry": geometry,
    }


def build_feature_collection(model, origin=(0.0, 0.0), include=None):
    """Convert a clean model into a GeoJSON FeatureCollection.

    `include` optionally limits the element types written, as a set drawn
    from {"Space", "Furniture", "Decor", "Structure"}.

    An item marked `_hidden` by `fs_clean.apply_edits` is left out. That is
    how a deletion made on the canvas reaches the file while staying
    reversible: the item is still in the model, so undoing brings it back.
    """
    project = Projector(origin)
    wanted = include or {"Space", "Furniture", "Decor", "Structure"}
    features = []

    # ---- Spaces ---------------------------------------------------------
    # Written first, so a reader meets the containers before the contents.
    if "Space" in wanted:
        for space in model.get("spaces", []):
            if space.get("_hidden"):
                continue
            capacity = space.get("capacity")
            features.append(_feature(
                {"type": "Polygon", "coordinates": project.rings(space["rings"])},
                "Space", space["cls"], space["sub_class"],
                feature_id=space["id"],
                name=space.get("name"),
                area=space.get("area"),
                center=project.point(space["center"]),
                capacity=0 if capacity is None else int(capacity),
            ))

    # ---- Furniture and decor -------------------------------------------
    for piece in model.get("furniture", []):
        if piece["element_type"] not in wanted or piece.get("_hidden"):
            continue
        features.append(_feature(
            {"type": "Polygon", "coordinates": project.rings(piece["rings"])},
            piece["element_type"], piece["cls"], piece["sub_class"],
            feature_id=piece["id"],
            space_id=piece.get("space_id"),
            custom_id=piece.get("custom_id"),
            # The reference leaves furniture area null and keeps only the
            # centre, so the same is done here.
            area=None,
            center=project.point(piece["center"]),
            capacity=piece.get("capacity"),
        ))

    # ---- Structure ------------------------------------------------------
    if "Structure" in wanted:
        # An opening is paired with its door, so they are written together.
        openings_by_door = {}
        for opening in model.get("openings", []):
            openings_by_door.setdefault(opening.get("door_id"), []).append(opening)

        for door in model.get("doors", []):
            if door.get("_hidden"):
                continue
            for opening in openings_by_door.get(door["id"], ()):
                features.append(_feature(
                    {"type": "Polygon",
                     "coordinates": [project.ring(opening["ring"])]},
                    "Structure", "Opening", None,
                    feature_id=opening.get("id"),
                ))
            paths = [project.ring(path) for path in door["paths"]
                     if len(path) >= 2]
            if not paths:
                continue
            features.append(_feature(
                {"type": "MultiLineString", "coordinates": paths},
                "Structure", "Door", None, feature_id=door["id"],
            ))

        for window in model.get("windows", []):
            if window.get("_hidden"):
                continue
            parts = [[project.ring(ring)] for ring in window["rings"]]
            if not parts:
                continue
            features.append(_feature(
                {"type": "MultiPolygon", "coordinates": parts},
                "Structure", window.get("cls") or "Window", None,
                feature_id=window["id"],
            ))

        # The wall mass is one feature for the whole floor, last in the file
        # and named "wallContours", exactly as the reference writes it.
        walls = model.get("walls") or []
        if walls:
            features.append(_feature(
                {"type": "MultiPolygon",
                 "coordinates": [project.rings(polygon) for polygon in walls]},
                "Structure", "Wall", None, feature_id="wallContours",
            ))

    return {"type": "FeatureCollection", "features": features}


def dumps(model, origin=(0.0, 0.0), include=None, indent=None):
    """Serialise a clean model to a GeoJSON string."""
    return json.dumps(build_feature_collection(model, origin, include),
                      indent=indent)


# ─────────────────────────────────────────────────────────────────────────────
# Validation
# ─────────────────────────────────────────────────────────────────────────────

REQUIRED_PROPERTIES = [
    "id", "spaceId", "groupId", "name", "customId", "customAttributes",
    "fsAttributes", "area", "center", "fill", "stroke",
    "fill-opacity", "stroke-opacity", "stroke-width",
]

GEOMETRY_BY_CLASS = {
    "Wall": {"MultiPolygon"},
    "Door": {"MultiLineString", "MultiPolygon"},
    "Window": {"MultiPolygon"},
    "Mullion": {"MultiPolygon"},
    "Opening": {"Polygon"},
}


def validate(collection, reference_terms_only=False):
    """Check a collection against the reference file's conventions.

    Returns a list of problems, empty when the file conforms. Used by the
    exporter's own tests and by the app before a download, so a malformed
    file is never handed to the user.
    """
    problems = []

    if collection.get("type") != "FeatureCollection":
        problems.append("top-level type is not FeatureCollection")
    features = collection.get("features")
    if not isinstance(features, list):
        return problems + ["features is not a list"]

    space_ids = set()
    for feature in features:
        props = feature.get("properties") or {}
        fs = props.get("fsAttributes") or {}
        if fs.get("elementType") == "Space":
            space_ids.add(props.get("id"))

    seen_ids = set()
    for index, feature in enumerate(features):
        where = "feature %d" % index
        props = feature.get("properties") or {}
        geometry = feature.get("geometry") or {}
        fs = props.get("fsAttributes") or {}

        for key in REQUIRED_PROPERTIES:
            if key not in props:
                problems.append("%s: missing property %s" % (where, key))

        element_type = fs.get("elementType")
        cls = fs.get("class")
        if element_type not in ("Space", "Furniture", "Structure", "Decor"):
            problems.append("%s: bad elementType %r" % (where, element_type))
        if not cls:
            problems.append("%s: missing class" % where)

        feature_id = props.get("id")
        if not feature_id:
            problems.append("%s: missing id" % where)
        elif feature_id in seen_ids:
            problems.append("%s: duplicate id %s" % (where, feature_id))
        else:
            seen_ids.add(feature_id)

        allowed = GEOMETRY_BY_CLASS.get(cls)
        if allowed and geometry.get("type") not in allowed:
            problems.append("%s: class %s has geometry %s, expected one of %s"
                            % (where, cls, geometry.get("type"),
                               sorted(allowed)))

        if element_type == "Space":
            if props.get("area") is None:
                problems.append("%s: space has no area" % where)
            if props.get("center") is None:
                problems.append("%s: space has no center" % where)
        elif element_type == "Structure":
            if props.get("area") is not None:
                problems.append("%s: structure should have null area" % where)

        space_id = props.get("spaceId")
        if space_id is not None and space_id not in space_ids:
            problems.append("%s: spaceId %s matches no space" % (where, space_id))

        if reference_terms_only and element_type and cls:
            if not tax.is_reference_term(element_type, cls, fs.get("subClass")):
                problems.append("%s: %s / %s / %s is not a reference term"
                                % (where, element_type, cls, fs.get("subClass")))

        problems.extend(_check_geometry(geometry, where))

    return problems


def _check_geometry(geometry, where):
    """Check one geometry's ring closure and minimum vertex counts."""
    problems = []
    kind = geometry.get("type")
    coords = geometry.get("coordinates")
    if not kind or coords is None:
        return ["%s: geometry missing type or coordinates" % where]

    def check_ring(ring, label):
        if len(ring) < 4:
            problems.append("%s: %s has only %d points" % (where, label, len(ring)))
            return
        if ring[0] != ring[-1]:
            problems.append("%s: %s is not closed" % (where, label))

    if kind == "Polygon":
        for i, ring in enumerate(coords):
            check_ring(ring, "ring %d" % i)
    elif kind == "MultiPolygon":
        for p, polygon in enumerate(coords):
            for i, ring in enumerate(polygon):
                check_ring(ring, "polygon %d ring %d" % (p, i))
    elif kind == "MultiLineString":
        for i, line in enumerate(coords):
            if len(line) < 2:
                problems.append("%s: line %d has %d points" % (where, i, len(line)))
    elif kind == "Point":
        if len(coords) < 2:
            problems.append("%s: point has %d ordinates" % (where, len(coords)))
    return problems


def compare_to_reference(collection, reference):
    """Summarise how an exported collection differs from a reference file.

    Reports the property and taxonomy vocabulary each side uses. Intended
    for a human to read while checking a new exporter, not for automated
    pass or fail.
    """
    def profile(coll):
        prop_keys, terms, geometries = set(), set(), set()
        for feature in coll.get("features", []):
            props = feature.get("properties") or {}
            fs = props.get("fsAttributes") or {}
            prop_keys.update(props.keys())
            terms.add((fs.get("elementType"), fs.get("class"), fs.get("subClass")))
            geometries.add((fs.get("elementType"), (feature.get("geometry") or {}).get("type")))
        return prop_keys, terms, geometries

    mine_props, mine_terms, mine_geom = profile(collection)
    ref_props, ref_terms, ref_geom = profile(reference)

    return {
        "properties_missing_vs_reference": sorted(ref_props - mine_props),
        "properties_extra_vs_reference": sorted(mine_props - ref_props),
        "terms_shared": sorted(str(t) for t in (mine_terms & ref_terms)),
        "terms_only_in_export": sorted(str(t) for t in (mine_terms - ref_terms)),
        "terms_only_in_reference": sorted(str(t) for t in (ref_terms - mine_terms)),
        "geometry_pairs_only_in_export": sorted(str(g) for g in (mine_geom - ref_geom)),
        "geometry_pairs_only_in_reference": sorted(str(g) for g in (ref_geom - mine_geom)),
        "counts": {"export": len(collection.get("features", [])),
                   "reference": len(reference.get("features", []))},
    }
