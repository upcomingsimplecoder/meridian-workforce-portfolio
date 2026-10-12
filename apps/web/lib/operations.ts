import type { components } from "../../../packages/contracts/api";
import { api, ApiError } from "./api";
export type Operations = components["schemas"]["OperationsView"];
function result<T>({
  data,
  error,
  response,
}: {
  data?: T;
  error?: { detail?: string };
  response: Response;
}): T {
  if (!response.ok || data === undefined)
    throw new ApiError(
      response.status,
      error?.detail || "Operational observations are unavailable.",
    );
  return data;
}
export async function getOperations() {
  return result(await api.GET("/api/v1/operations"));
}
export async function getTrace(trace_id: string) {
  return result(
    await api.GET("/api/v1/operations/traces/{trace_id}", {
      params: { path: { trace_id } },
    }),
  );
}
