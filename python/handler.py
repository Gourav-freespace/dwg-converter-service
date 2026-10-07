"""DXF -> Freespace GeoJSON Lambda handler and CLI.

Runs the floorplan-editor engine (copied verbatim under `engine/`) outside
Pyodide. The pipeline is:

    fs_clean.build_clean_model(dxf_bytes)  ->  fs_geojson.dumps(model)

Two entry points share the same conversion code:

- `lambda_handler`: invoked by the Node API lambda. The DXF arrives via S3
  (`bucket` + `key`), because DXF files exceed the 6 MB invoke payload
  limit. The GeoJSON is written back to S3 and returned inline when small.
- CLI (`python handler.py --dxf in.dxf --out out.geojson`): used for local
  development and tests; the Node service spawns this when
  GEOJSON_PY_LOCAL=1.
"""

import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
# Vendored manylinux wheels (built by scripts/build-python-deps.sh) first,
# then the engine modules.
for p in ("vendor", "engine"):
    full = os.path.join(_HERE, p)
    if full not in sys.path:
        sys.path.insert(0, full)

import fs_clean      # noqa: E402
import fs_geojson    # noqa: E402

# Inline the GeoJSON in the invoke response only when it fits comfortably
# under Lambda's 6 MB response limit.
INLINE_LIMIT_BYTES = 4 * 1024 * 1024


def convert_dxf_to_geojson(dxf_bytes, origin=(0.0, 0.0), unit_override=None,
                           allow_recovery=True):
    """Run the full pipeline. Returns (geojson_str, model_summary).

    Raises ValueError with a user-readable sentence when the drawing cannot
    be converted; the engine writes every refusal that way.
    """
    model = fs_clean.build_clean_model(
        bytes(dxf_bytes),
        unit_override=float(unit_override) if unit_override else None,
        measure_units=not unit_override,
        allow_recovery=bool(allow_recovery),
    )
    geojson = fs_geojson.dumps(model, origin=origin)
    stats = model.get("stats") or {}
    summary = {
        "warnings": model.get("warnings") or [],
        "stats": {k: v for k, v in stats.items()
                  if isinstance(v, (int, float, str, bool))},
    }
    return geojson, summary


def lambda_handler(event, context):  # noqa: ARG001 (AWS signature)
    import boto3  # provided by the Lambda runtime

    bucket = event.get("bucket")
    key = event.get("key")
    if not bucket or not key:
        return {"success": False,
                "error": "Event needs 'bucket' and 'key' for the input DXF."}

    s3 = boto3.client("s3")
    dxf_bytes = s3.get_object(Bucket=bucket, Key=key)["Body"].read()

    try:
        geojson, summary = convert_dxf_to_geojson(
            dxf_bytes,
            origin=tuple(event.get("origin") or (0.0, 0.0)),
            unit_override=event.get("unit_override"),
            allow_recovery=event.get("allow_recovery", True),
        )
    except ValueError as err:
        # Engine refusals are user-readable sentences; pass them through.
        return {"success": False, "error": str(err)}

    output_key = event.get("output_key") or (
        key.rsplit(".", 1)[0] + ".geojson")
    s3.put_object(Bucket=bucket, Key=output_key,
                  Body=geojson.encode("utf-8"),
                  ContentType="application/geo+json")

    result = {
        "success": True,
        "geojson_s3_key": output_key,
        "geojson_size_bytes": len(geojson),
        **summary,
    }
    if len(geojson) <= INLINE_LIMIT_BYTES:
        result["geojson"] = geojson
    return result


def _cli():
    parser = argparse.ArgumentParser(description="DXF -> Freespace GeoJSON")
    parser.add_argument("--dxf", required=True, help="input DXF path")
    parser.add_argument("--out", required=True, help="output GeoJSON path")
    parser.add_argument("--origin", default="0,0",
                        help="lat,lon to georeference (default 0,0)")
    parser.add_argument("--unit-override", type=float, default=None)
    parser.add_argument("--no-recovery", action="store_true")
    args = parser.parse_args()

    with open(args.dxf, "rb") as fh:
        dxf_bytes = fh.read()

    lat, lon = (float(v) for v in args.origin.split(","))
    try:
        geojson, summary = convert_dxf_to_geojson(
            dxf_bytes, origin=(lat, lon),
            unit_override=args.unit_override,
            allow_recovery=not args.no_recovery,
        )
    except ValueError as err:
        print(json.dumps({"success": False, "error": str(err)}))
        sys.exit(1)

    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(geojson)
    print(json.dumps({
        "success": True,
        "out": args.out,
        "geojson_size_bytes": len(geojson),
        **summary,
    }))


if __name__ == "__main__":
    _cli()
