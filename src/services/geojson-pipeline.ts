import { execFile } from "child_process";
import { promises as fs } from "fs";
import os from "os";
import path from "path";
import {
  GetObjectCommand,
  PutObjectCommand,
  S3Client,
} from "@aws-sdk/client-s3";
import { InvokeCommand, LambdaClient } from "@aws-sdk/client-lambda";
import { DwgConversionError } from "./dwg-converter-service";

const PIPELINE_TIMEOUT_MS = 14 * 60 * 1000;

export interface GeojsonPipelineResult {
  geojson: string;
  warnings: string[];
  stats: Record<string, unknown>;
  s3DxfKey: string | null;
  s3GeojsonKey: string | null;
}

interface PythonSummary {
  success: boolean;
  error?: string;
  geojson?: string;
  geojson_s3_key?: string;
  geojson_size_bytes?: number;
  warnings?: string[];
  stats?: Record<string, unknown>;
}

const isLocalMode = (): boolean => process.env.GEOJSON_PY_LOCAL === "1";

// ---------------------------------------------------------------------------
// Local driver: spawn the Python CLI (python/handler.py). Used in
// development, where the engine's venv lives next to the repo.
// ---------------------------------------------------------------------------

const runLocalPython = async (dxf: Buffer): Promise<PythonSummary & { geojson?: string }> => {
  const pythonBin = process.env.PYTHON_BIN ?? "python3";
  // cwd-relative (not __dirname): esbuild bundles this file away from the
  // repo layout, and local mode always runs from the repo root.
  const handler =
    process.env.GEOJSON_PY_HANDLER ??
    path.resolve(process.cwd(), "python/handler.py");
  const workDir = await fs.mkdtemp(path.join(os.tmpdir(), "dxf2geo-"));
  const inFile = path.join(workDir, "input.dxf");
  const outFile = path.join(workDir, "output.geojson");
  try {
    await fs.writeFile(inFile, dxf);
    const stdout = await new Promise<string>((resolve, reject) => {
      execFile(
        pythonBin,
        [handler, "--dxf", inFile, "--out", outFile],
        { timeout: PIPELINE_TIMEOUT_MS, maxBuffer: 16 * 1024 * 1024 },
        (error, out, stderr) => {
          if (error && !out) {
            reject(
              new DwgConversionError(
                `GeoJSON pipeline failed: ${error.message}. stderr: ${stderr.slice(0, 2000)}`
              )
            );
            return;
          }
          resolve(out);
        }
      );
    });
    const summary = JSON.parse(stdout.trim().split("\n").pop() ?? "{}") as PythonSummary;
    if (!summary.success) {
      throw new DwgConversionError(
        summary.error ?? "GeoJSON pipeline failed", 422
      );
    }
    summary.geojson = await fs.readFile(outFile, "utf-8");
    return summary;
  } finally {
    await fs.rm(workDir, { recursive: true, force: true });
  }
};

// ---------------------------------------------------------------------------
// Lambda driver: hand the DXF over via S3 (it exceeds the 6 MB invoke
// payload limit) and invoke the Python function.
// ---------------------------------------------------------------------------

const runLambdaPython = async (
  dxf: Buffer,
  baseName: string
): Promise<PythonSummary & { dxfKey: string }> => {
  const bucket = process.env.DXF_S3_BUCKET;
  if (!bucket) {
    throw new DwgConversionError(
      "DXF_S3_BUCKET is not configured; the GeoJSON pipeline needs it to hand the DXF to the Python lambda."
    );
  }
  const region = process.env.DXF_S3_BUCKET_REGION ?? process.env.REGION;
  const functionName =
    process.env.GEOJSON_LAMBDA_NAME ?? `${process.env.STAGE}-dxf-to-geojson`;

  const stamp = Date.now();
  const dxfKey = `dwg-conversions/${stamp}-${baseName}.dxf`;
  const geojsonKey = `dwg-conversions/${stamp}-${baseName}.geojson`;

  const s3 = new S3Client({ region });
  await s3.send(
    new PutObjectCommand({ Bucket: bucket, Key: dxfKey, Body: dxf })
  );

  const lambda = new LambdaClient({ region: process.env.REGION });
  const invocation = await lambda.send(
    new InvokeCommand({
      FunctionName: functionName,
      Payload: Buffer.from(
        JSON.stringify({ bucket, key: dxfKey, output_key: geojsonKey })
      ),
    })
  );
  if (invocation.FunctionError) {
    throw new DwgConversionError(
      `GeoJSON lambda errored: ${Buffer.from(invocation.Payload ?? []).toString().slice(0, 2000)}`
    );
  }
  const summary = JSON.parse(
    Buffer.from(invocation.Payload ?? []).toString()
  ) as PythonSummary;
  if (!summary.success) {
    throw new DwgConversionError(
      summary.error ?? "GeoJSON pipeline failed", 422
    );
  }

  // Large GeoJSON is not inlined in the invoke response; fetch it from S3.
  if (!summary.geojson && summary.geojson_s3_key) {
    const obj = await s3.send(
      new GetObjectCommand({ Bucket: bucket, Key: summary.geojson_s3_key })
    );
    summary.geojson = await obj.Body?.transformToString("utf-8");
  }
  return { ...summary, dxfKey };
};

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

export const dxfToGeojson = async (
  dxf: Buffer,
  baseName: string
): Promise<GeojsonPipelineResult> => {
  if (isLocalMode()) {
    const summary = await runLocalPython(dxf);
    return {
      geojson: summary.geojson ?? "",
      warnings: summary.warnings ?? [],
      stats: summary.stats ?? {},
      s3DxfKey: null,
      s3GeojsonKey: null,
    };
  }
  const summary = await runLambdaPython(dxf, baseName);
  if (!summary.geojson) {
    throw new DwgConversionError(
      "GeoJSON pipeline returned no output document."
    );
  }
  return {
    geojson: summary.geojson,
    warnings: summary.warnings ?? [],
    stats: summary.stats ?? {},
    s3DxfKey: summary.dxfKey,
    s3GeojsonKey: summary.geojson_s3_key ?? null,
  };
};
