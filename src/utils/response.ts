import { APIGatewayProxyResult } from "aws-lambda";

export const response = (
  statusCode: number,
  body: object
): APIGatewayProxyResult => ({
  statusCode,
  headers: {
    "content-type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "*",
    "Access-Control-Allow-Headers": "*",
  },
  body: JSON.stringify(body),
});
