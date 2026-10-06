# dwg-converter Lambda layer

This folder is packaged by `serverless.yml` (`layers.dwgConverter`) and
mounted in the Lambda at `/opt`. The function expects the converter at
`/opt/bin/dwg2dxf` (`DWG_CONVERTER_PATH`).

## Contents

```
bin/dwg2dxf   <- LibreDWG 0.14 dwg2dxf, compiled for Amazon Linux x86_64
```

The binary is NOT in git by default. Build it once before the first
deploy (and after any LibreDWG upgrade):

```bash
./scripts/build-dwg-layer.sh
```

The script needs Docker. It compiles LibreDWG 0.14 inside an
`amazonlinux:2023` container for `linux/amd64` (the stack's Lambda
architecture) and writes the binary to `layers/dwg-converter/bin/dwg2dxf`.

Deploying without the binary creates an empty layer. Conversions then
fail with a clear error:
`DWG_CONVERTER_PATH is set to "/opt/bin/dwg2dxf" but no file exists there`.

## Why a layer

The service's esbuild bundle is JavaScript only. The converter is a
native C program, so it ships as a layer. The npm WASM fallback
(`dwg2dxf-converter`, LibreDWG 0.13.4) is NOT acceptable as primary: it
silently dropped all TEXT entities and a third of the space polygons on
real drawings. See `docs/dwg-to-dxf-api.md`.
