import { APIGatewayProxyEvent, APIGatewayProxyResult } from "aws-lambda";
import { parse } from "aws-multipart-parser";
import { promises as fs } from "fs";
import os from "os";
import path from "path";
import { response } from "../utils/response";
import { s3Upload } from "../utils/s3";
import {
  convertDwgToDxf,
  DwgConversionError,
  isDwgBuffer,
} from "../services/dwg-converter-service";

interface ParsedFile {
  filename: string;
  content: Buffer;
  contentType: string;
}

// API Gateway caps responses at 10 MB; stay well under it for inline output.
const INLINE_LIMIT_BYTES = 5 * 1024 * 1024;

// Local stash for converted DXFs. Temporary by design: the OS (or the Lambda
// container recycle) cleans it up. Override with DXF_STASH_DIR.
const stashDir = (): string =>
  process.env.DXF_STASH_DIR ?? path.join(os.tmpdir(), "dwg-dxf-stash");

const stashDxf = async (
  baseName: string,
  dxf: Buffer
): Promise<string | null> => {
  try {
    const dir = stashDir();
    await fs.mkdir(dir, { recursive: true });
    const localPath = path.join(dir, `${Date.now()}-${baseName}.dxf`);
    await fs.writeFile(localPath, dxf);
    return localPath;
  } catch {
    // The stash is a convenience copy; never fail the request over it.
    return null;
  }
};

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "*",
  "Access-Control-Allow-Headers": "*",
};

const convertEndpoint = async (
  event: APIGatewayProxyEvent
): Promise<APIGatewayProxyResult> => {
  let file: ParsedFile | undefined;
  try {
    const parsedData = parse(event, true);
    file = parsedData.file as ParsedFile;
  } catch (parseError) {
    console.error("Error parsing multipart data:", parseError);
    return response(400, {
      success: false,
      error: "Invalid file upload",
      details: "Could not parse uploaded file.",
    });
  }

  if (!file || !file.content) {
    return response(400, {
      success: false,
      error: "No file uploaded",
      details: 'Send the DWG as multipart form-data under the field name "file".',
    });
  }

  const content = Buffer.isBuffer(file.content)
    ? file.content
    : Buffer.from(file.content);

  if (!file.filename?.toLowerCase().endsWith(".dwg") || !isDwgBuffer(content)) {
    return response(400, {
      success: false,
      error: "File must be a DWG drawing",
      details: "The file needs a .dwg extension and a valid DWG header (AC10xx).",
    });
  }

  try {
    console.info(
      `Converting DWG to DXF: ${file.filename} (${content.length} bytes)`
    );
    const result = await convertDwgToDxf(content, file.filename);
    console.info(
      `Converted ${file.filename} with ${result.converter}: DXF is ${result.dxf.length} bytes`
    );

    const baseName = file.filename.replace(/\.dwg$/i, "");

    // Keep a temporary local copy regardless of output mode.
    const localPath = await stashDxf(baseName, result.dxf);
    if (localPath) {
      console.info(`DXF stashed locally at ${localPath}`);
    } else {
      console.warn(`Could not stash DXF locally for ${baseName}.dxf`);
    }

    // Inline output for small results, when the caller asks for it.
    if (
      event.queryStringParameters?.output === "inline" &&
      result.dxf.length <= INLINE_LIMIT_BYTES
    ) {
      return {
        statusCode: 200,
        headers: {
          "content-type": "application/dxf",
          "content-disposition": `attachment; filename="${baseName}.dxf"`,
          "x-dxf-local-path": localPath ?? "",
          ...corsHeaders,
        },
        body: result.dxf.toString("utf-8"),
      };
    }

    const s3Key = `dwg-conversions/${Date.now()}-${baseName}.dxf`;
    const upload = await s3Upload(s3Key, result.dxf.toString("utf-8"));
    if (!upload.success) {
      // The conversion itself succeeded and the DXF is stashed locally, so
      // report partial success instead of a hard failure.
      console.error(`S3 upload failed for ${baseName}.dxf: ${upload.error}`);
      return response(200, {
        success: true,
        filename: `${baseName}.dxf`,
        dwgVersion: result.dwgVersion,
        converter: result.converter,
        dxfSizeBytes: result.dxf.length,
        localPath,
        s3Key: null,
        s3Url: null,
        warning: `S3 upload failed: ${upload.error}. The DXF is only in the local stash.`,
      });
    }

    return response(200, {
      success: true,
      filename: `${baseName}.dxf`,
      dwgVersion: result.dwgVersion,
      converter: result.converter,
      dxfSizeBytes: result.dxf.length,
      localPath,
      s3Key: upload.s3Key,
      s3Url: upload.s3Url,
    });
  } catch (error) {
    if (error instanceof DwgConversionError) {
      console.error(`DWG conversion failed: ${error.message}`);
      return response(error.statusCode, {
        success: false,
        error: "DWG to DXF conversion failed",
        details: error.message,
      });
    }
    console.error("Unexpected error during DWG conversion:", error);
    return response(500, {
      success: false,
      error: "Internal server error",
      details: error instanceof Error ? error.message : String(error),
    });
  }
};

export const dwgConverterHandler = async (
  event: APIGatewayProxyEvent
): Promise<APIGatewayProxyResult> => {
  if (event.httpMethod === "OPTIONS") {
    return { statusCode: 200, headers: corsHeaders, body: "" };
  }
  if (event.httpMethod === "POST") {
    return convertEndpoint(event);
  }
  return response(405, { success: false, error: "Method not allowed" });
};
