# dwg-converter-service

Standalone serverless API that converts AutoCAD DWG drawings to DXF.
This is step one of the DWG -> DXF -> Freespace GeoJSON pipeline. The DXF
output feeds the floorplan-editor engine.

## API

`POST /convert-dwg-to-dxf`

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
cp .env.example .env
npm run offline
```

```bash
curl -X POST "http://localhost:3000/convert-dwg-to-dxf?output=inline" \
  -H "Authorization: Bearer <jwt>" \
  -F "file=@DWG/your-drawing.dwg" -o converted.dxf
```

Run tests with `npm test`. The real-drawing test is skipped when no DWG
file is present in `DWG/` (drawings are gitignored).

## Deployment

The native binary ships as a Lambda layer from `layers/dwg-converter/`
(mounted at `/opt`; `DWG_CONVERTER_PATH` defaults to `/opt/bin/dwg2dxf`).
Build the binary once before the first deploy:

```bash
npm run build:layer   # requires Docker; compiles in amazonlinux:2023
```

Then set `DXF_S3_BUCKET` and deploy:

```bash
npm run deploy:qa
```

Deploying without the binary creates an empty layer and conversions fail
with a clear config error. The WASM fallback deliberately does not kick
in when `DWG_CONVERTER_PATH` points at a missing file.
