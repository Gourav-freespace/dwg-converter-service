import { execFile } from "child_process";
import { promises as fs } from "fs";
import os from "os";
import path from "path";

const CONVERSION_TIMEOUT_MS = 10 * 60 * 1000;

// DWG files start with a 6-byte version code, e.g. AC1032 (AutoCAD 2018).
const DWG_MAGIC = "AC10";

export interface DwgConversionResult {
  dxf: Buffer;
  converter: string;
  dwgVersion: string;
}

export class DwgConversionError extends Error {
  statusCode: number;

  constructor(message: string, statusCode = 500) {
    super(message);
    this.name = "DwgConversionError";
    this.statusCode = statusCode;
  }
}

export const isDwgBuffer = (data: Buffer): boolean =>
  data.length > 6 && data.subarray(0, 4).toString("ascii") === DWG_MAGIC;

export const readDwgVersion = (data: Buffer): string =>
  data.subarray(0, 6).toString("ascii");

const fileExists = async (p: string): Promise<boolean> => {
  try {
    await fs.access(p);
    return true;
  } catch {
    return false;
  }
};

const findOnPath = async (name: string): Promise<string | null> => {
  const dirs = (process.env.PATH ?? "").split(path.delimiter);
  for (const dir of dirs) {
    if (!dir) continue;
    const candidate = path.join(dir, name);
    if (await fileExists(candidate)) return candidate;
  }
  return null;
};

// ---------------------------------------------------------------------------
// WASM engine (dwg2dxf-converter: LibreDWG compiled to WebAssembly).
// LAST-RESORT FALLBACK ONLY. It embeds LibreDWG 0.13.4, which silently drops
// entities: on a real drawing it lost all 479 TEXT labels and 76 of 239
// space polygons that the native 0.14 binary preserved. Prefer a binary.
// ---------------------------------------------------------------------------

interface WasmConversionResult {
  success: boolean;
  outputPath: string | null;
  error: string | null;
}

type WasmConvertFn = (
  inputPath: string,
  outputPath: string,
  options?: { timeout?: number }
) => Promise<WasmConversionResult>;

export const loadWasmConverter = (): WasmConvertFn | null => {
  try {
    // Required lazily so a missing/unbundled package degrades to the
    // binary fallback instead of crashing the whole Lambda at import time.
    // eslint-disable-next-line @typescript-eslint/no-var-requires, global-require
    const mod = require("dwg2dxf-converter");
    return typeof mod?.convertDwgToDxf === "function"
      ? (mod.convertDwgToDxf as WasmConvertFn)
      : null;
  } catch {
    return null;
  }
};

// ---------------------------------------------------------------------------
// External binary engines (preferred).
// ---------------------------------------------------------------------------

/**
 * Resolves a converter binary. Order:
 * 1. DWG_CONVERTER_PATH env var (absolute path, explicit override).
 *    If set but missing, this is a config error and conversion fails.
 * 2. ODAFileConverter on PATH
 * 3. dwg2dxf (LibreDWG) on PATH
 * Returns null when nothing is configured or found, so the caller can
 * fall back to the WASM engine.
 */
export const resolveConverter = async (): Promise<string | null> => {
  const fromEnv = process.env.DWG_CONVERTER_PATH;
  if (fromEnv) {
    if (await fileExists(fromEnv)) return fromEnv;
    throw new DwgConversionError(
      `DWG_CONVERTER_PATH is set to "${fromEnv}" but no file exists there`
    );
  }
  const oda = await findOnPath("ODAFileConverter");
  if (oda) return oda;
  const libredwg = await findOnPath("dwg2dxf");
  if (libredwg) return libredwg;
  return null;
};

const run = (
  bin: string,
  args: string[],
  cwd: string
): Promise<{ stdout: string; stderr: string }> =>
  new Promise((resolve, reject) => {
    execFile(
      bin,
      args,
      { cwd, timeout: CONVERSION_TIMEOUT_MS, maxBuffer: 64 * 1024 * 1024 },
      (error, stdout, stderr) => {
        if (error) {
          reject(
            new DwgConversionError(
              `Converter failed: ${error.message}. stderr: ${stderr.slice(0, 2000)}`
            )
          );
          return;
        }
        resolve({ stdout, stderr });
      }
    );
  });

/**
 * ODA File Converter works on directories:
 *   ODAFileConverter <inDir> <outDir> <outVersion> DXF <recurse> <audit>
 */
const runOdaConverter = async (
  bin: string,
  inDir: string,
  outDir: string
): Promise<void> => {
  await run(bin, [inDir, outDir, "ACAD2018", "DXF", "0", "1"], inDir);
};

/**
 * LibreDWG dwg2dxf works on single files:
 *   dwg2dxf -y -o <out.dxf> <in.dwg>
 */
const runDwg2dxf = async (
  bin: string,
  inFile: string,
  outFile: string
): Promise<void> => {
  await run(bin, ["-y", "-o", outFile, inFile], path.dirname(inFile));
};

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

export const convertDwgToDxf = async (
  dwg: Buffer,
  originalFilename: string
): Promise<DwgConversionResult> => {
  if (!isDwgBuffer(dwg)) {
    throw new DwgConversionError(
      "File is not a DWG: missing AC10xx header",
      400
    );
  }

  const workDir = await fs.mkdtemp(path.join(os.tmpdir(), "dwg2dxf-"));
  const inDir = path.join(workDir, "in");
  const outDir = path.join(workDir, "out");
  await fs.mkdir(inDir);
  await fs.mkdir(outDir);

  // Keep a predictable, shell-safe name inside the temp dir.
  const safeBase = "input";
  const inFile = path.join(inDir, `${safeBase}.dwg`);
  const outFile = path.join(outDir, `${safeBase}.dxf`);

  try {
    await fs.writeFile(inFile, dwg);

    let converterName: string;

    // Binary first: the WASM engine (LibreDWG 0.13.4) drops entities, so it
    // only runs when no binary is configured or installed.
    const binary = await resolveConverter();

    if (binary) {
      converterName = path.basename(binary);
      if (converterName.toLowerCase().includes("odafileconverter")) {
        await runOdaConverter(binary, inDir, outDir);
      } else {
        await runDwg2dxf(binary, inFile, outFile);
      }
    } else {
      const wasmConvert = loadWasmConverter();
      if (!wasmConvert) {
        throw new DwgConversionError(
          "No DWG converter available. Set DWG_CONVERTER_PATH, install " +
            "ODAFileConverter or dwg2dxf, or add the dwg2dxf-converter package."
        );
      }
      converterName = "dwg2dxf-wasm";
      const result = await wasmConvert(inFile, outFile, {
        timeout: CONVERSION_TIMEOUT_MS,
      });
      if (!result.success) {
        throw new DwgConversionError(
          `WASM converter failed for ${originalFilename}: ${result.error ?? "unknown error"}`
        );
      }
    }

    if (!(await fileExists(outFile))) {
      throw new DwgConversionError(
        `Converter produced no output for ${originalFilename}`
      );
    }
    const dxf = await fs.readFile(outFile);
    if (dxf.length === 0) {
      throw new DwgConversionError(
        `Converter produced an empty DXF for ${originalFilename}`
      );
    }
    return { dxf, converter: converterName, dwgVersion: readDwgVersion(dwg) };
  } finally {
    await fs.rm(workDir, { recursive: true, force: true });
  }
};
