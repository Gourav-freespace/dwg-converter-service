import { S3Client, PutObjectCommand } from "@aws-sdk/client-s3";

export interface S3UploadResult {
  success: boolean;
  s3Key?: string;
  s3Url?: string;
  error?: string;
}

export const s3Upload = async (
  s3Key: string,
  body: string | Buffer
): Promise<S3UploadResult> => {
  const bucket = process.env.DXF_S3_BUCKET;
  const region = process.env.DXF_S3_BUCKET_REGION ?? process.env.REGION;
  if (!bucket) {
    return { success: false, error: "DXF_S3_BUCKET is not configured" };
  }
  try {
    const s3 = new S3Client({ region });
    await s3.send(
      new PutObjectCommand({ Bucket: bucket, Key: s3Key, Body: body })
    );
    return {
      success: true,
      s3Key,
      s3Url: `https://${bucket}.s3.${region}.amazonaws.com/${s3Key}`,
    };
  } catch (error) {
    return {
      success: false,
      error: error instanceof Error ? error.message : String(error),
    };
  }
};
