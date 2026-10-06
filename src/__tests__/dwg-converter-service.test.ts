import { promises as fs } from "fs";
import os from "os";
import path from "path";
import {
  convertDwgToDxf,
  DwgConversionError,
  isDwgBuffer,
  loadWasmConverter,
  readDwgVersion,
  resolveConverter,
} from "../services/dwg-converter-service";

// A minimal buffer that passes the DWG header check (AC1032 = AutoCAD 2018).
const fakeDwg = Buffer.concat([
  Buffer.from("AC1032", "ascii"),
  Buffer.alloc(64),
]);

describe("dwg-converter-service", () => {
  let stubDir: string;
  const originalEnv = process.env.DWG_CONVERTER_PATH;

  beforeAll(async () => {
    // Stub converter: a shell script with the dwg2dxf CLI shape
    // (dwg2dxf -y -o <out.dxf> <in.dwg>) that writes a fixed DXF.
    stubDir = await fs.mkdtemp(path.join(os.tmpdir(), "dwg-stub-"));
    const stub = path.join(stubDir, "dwg2dxf");
    await fs.writeFile(
      stub,
      '#!/bin/sh\nprintf "0\\nSECTION\\n2\\nENTITIES\\n0\\nENDSEC\\n0\\nEOF\\n" > "$3"\n',
      { mode: 0o755 }
    );
  });

  afterAll(async () => {
    await fs.rm(stubDir, { recursive: true, force: true });
  });

  afterEach(() => {
    process.env.DWG_CONVERTER_PATH = originalEnv;
  });

  describe("isDwgBuffer / readDwgVersion", () => {
    it("accepts a buffer with an AC10xx header", () => {
      expect(isDwgBuffer(fakeDwg)).toBe(true);
      expect(readDwgVersion(fakeDwg)).toBe("AC1032");
    });

    it("rejects non-DWG content", () => {
      expect(isDwgBuffer(Buffer.from("not a dwg file at all"))).toBe(false);
      expect(isDwgBuffer(Buffer.alloc(0))).toBe(false);
    });
  });

  describe("resolveConverter", () => {
    it("uses DWG_CONVERTER_PATH when it points to a real file", async () => {
      process.env.DWG_CONVERTER_PATH = path.join(stubDir, "dwg2dxf");
      await expect(resolveConverter()).resolves.toBe(
        path.join(stubDir, "dwg2dxf")
      );
    });

    it("fails clearly when DWG_CONVERTER_PATH points nowhere", async () => {
      process.env.DWG_CONVERTER_PATH = "/no/such/binary";
      await expect(resolveConverter()).rejects.toThrow(DwgConversionError);
    });
  });

  describe("convertDwgToDxf (binary override)", () => {
    it("rejects input without a DWG header", async () => {
      await expect(
        convertDwgToDxf(Buffer.from("garbage"), "x.dwg")
      ).rejects.toThrow("not a DWG");
    });

    it("uses the DWG_CONVERTER_PATH binary when set", async () => {
      process.env.DWG_CONVERTER_PATH = path.join(stubDir, "dwg2dxf");
      const result = await convertDwgToDxf(fakeDwg, "plan.dwg");
      expect(result.converter).toBe("dwg2dxf");
      expect(result.dwgVersion).toBe("AC1032");
      expect(result.dxf.toString("utf-8")).toContain("ENTITIES");
    });

    it("fails clearly when the converter produces no output", async () => {
      const silent = path.join(stubDir, "dwg2dxf-silent");
      await fs.writeFile(silent, "#!/bin/sh\nexit 0\n", { mode: 0o755 });
      process.env.DWG_CONVERTER_PATH = silent;
      await expect(convertDwgToDxf(fakeDwg, "plan.dwg")).rejects.toThrow(
        "produced no output"
      );
    });
  });

  describe("convertDwgToDxf (WASM fallback)", () => {
    const originalPath = process.env.PATH;

    const forceWasm = () => {
      // No override and no binaries findable -> the WASM fallback must run.
      delete process.env.DWG_CONVERTER_PATH;
      process.env.PATH = "";
    };

    afterEach(() => {
      process.env.PATH = originalPath;
    });

    it("loads the WASM engine", () => {
      expect(loadWasmConverter()).not.toBeNull();
    });

    it("prefers a PATH binary over WASM", async () => {
      delete process.env.DWG_CONVERTER_PATH;
      process.env.PATH = stubDir;
      const result = await convertDwgToDxf(fakeDwg, "plan.dwg");
      expect(result.converter).toBe("dwg2dxf");
    });

    it("rejects a fake DWG with a clear WASM error", async () => {
      forceWasm();
      // Header is valid but the body is garbage, so the engine must fail.
      await expect(convertDwgToDxf(fakeDwg, "plan.dwg")).rejects.toThrow(
        "WASM converter failed"
      );
    });

    it("converts a real DWG without any binary installed", async () => {
      const realDwg = path.join(
        __dirname,
        "../../DWG/FS_198_6008_Mod 3 Level 1.dwg"
      );
      let real: Buffer;
      try {
        real = await fs.readFile(realDwg);
      } catch {
        console.warn(`skipped: real drawing not found at ${realDwg}`);
        return;
      }
      forceWasm();
      const result = await convertDwgToDxf(real, "FS_198.dwg");
      expect(result.converter).toBe("dwg2dxf-wasm");
      expect(result.dwgVersion).toBe("AC1032");
      expect(result.dxf.length).toBeGreaterThan(1_000_000);
      expect(result.dxf.includes("ENTITIES")).toBe(true);
    }, 60_000);
  });
});
