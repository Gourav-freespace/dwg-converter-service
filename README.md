# dwg-converter-service

Standalone serverless API that converts AutoCAD DWG drawings to DXF and
to Freespace GeoJSON. The GeoJSON pipeline runs the floorplan-editor
engine (copied verbatim under `python/engine/`) in a Python lambda.

## API

### `POST /convert-dwg-to-geojson`

The full pipeline: DWG -> DXF (native converter) -> Freespace GeoJSON
(floorplan engine: unit detection, wall reconstruction, room tracing,
furniture classification).

- Body: `multipart/form-data`, DWG under the field name `file`.
- Default response: JSON envelope with `geojson` (the FeatureCollection,
  inlined when under 5 MB), `stats` (spaces, desks, seats, walls...),
  `warnings` from the engine, sizes, and the S3 keys / local stash path.
- `?output=inline`: returns the raw FeatureCollection body
  (`application/geo+json`) when it fits.
- A drawing the engine cannot convert returns 422 with the engine's own
  user-readable reason.

Architecture: the Node lambda converts DWG -> DXF, uploads the DXF to S3,
and invokes the `dxf-to-geojson` Python lambda (`python/handler.py`),
which runs the engine and writes the GeoJSON back to S3. Locally
(`GEOJSON_PY_LOCAL=1`) the Node code spawns the same handler as a CLI via
`PYTHON_BIN` instead, so no AWS is needed.

### `POST /convert-dwg-to-dxf`

- Body: `multipart/form-data` with the drawing under the field name `file`.
- The file must have a `.dwg` extension and a valid DWG header (`AC10xx`).
- Auth: reuses the vector-service token authorizer (same Freespace JWT).

Default response: JSON with `s3Key`/`s3Url` (the DXF is uploaded to
`DXF_S3_BUCKET`), plus `localPath` (a temporary stash copy on the
instance) and `converter` (which engine ran). With `?output=inline` the
DXF text is returned directly when under 5 MB.

S3 is the default because DXF output is large: an 8 MB DWG produced an
80 MB DXF, which exceeds API Gateway's 10 MB response limit. If the S3
upload fails, the endpoint still returns 200 with `localPath` set and a
`warning`.

## Conversion engine

Primary engine: a native **LibreDWG 0.14 `dwg2dxf`** binary. Selection
order (see `src/services/dwg-converter-service.ts`):

1. `DWG_CONVERTER_PATH` (absolute path; supports dwg2dxf and
   ODAFileConverter CLI styles).
2. `ODAFileConverter`, then `dwg2dxf`, found on `PATH`.
3. Last resort: the `dwg2dxf-converter` npm package (LibreDWG 0.13.4 as
   WASM). WARNING: 0.13.4 silently drops entities (all TEXT labels and a
   third of the space polygons on a real drawing). The `converter` field
   says `dwg2dxf-wasm` when this degraded engine ran.

Licensing: LibreDWG is GPL. Server-side internal use is fine; review
before redistributing.

## Local development

```bash
npm install
brew install libredwg
python3 -m venv .venv && .venv/bin/pip install -r python/requirements.txt
cp .env.example .env   # adjust PYTHON_BIN if needed
npm run offline
```

```bash
curl -X POST "http://localhost:3000/convert-dwg-to-geojson" \
  -F "file=@DWG/your-drawing.dwg"
```

```bash
curl -X POST "http://localhost:3000/convert-dwg-to-dxf?output=inline" \
  -H "Authorization: Bearer <jwt>" \
  -F "file=@DWG/your-drawing.dwg" -o converted.dxf
```

Run tests with `npm test`. The real-drawing test is skipped when no DWG
file is present in `DWG/` (drawings are gitignored).

## Deployment

Two one-time build steps before the first deploy:

```bash
npm run build:layer              # dwg2dxf binary (requires Docker)
./scripts/build-python-deps.sh   # vendors ezdxf+numpy manylinux wheels
```

The native binary ships as a Lambda layer from `layers/dwg-converter/`
(mounted at `/opt`; `DWG_CONVERTER_PATH` defaults to `/opt/bin/dwg2dxf`).
The Python lambda packages `python/` with the vendored wheels in
`python/vendor/`.

Then set `DXF_S3_BUCKET` (required by the GeoJSON pipeline for the
DXF hand-off between lambdas) and deploy:

```bash
npm run deploy:qa
```

Deploying without the binary creates an empty layer and conversions fail
with a clear config error. The WASM fallback deliberately does not kick
in when `DWG_CONVERTER_PATH` points at a missing file.
