import type { components } from "../../../packages/contracts/api";
import { api, ApiError, type Me } from "./api";
export type AnalyticalRun = components["schemas"]["AnalyticsRunView"];
export type AnalyticalRequest = components["schemas"]["AnalyticsRequest"];
export type AnalyticalOptions = components["schemas"]["AnalyticsOptions"];
function result<T>({
  data,
  error,
  response,
}: {
  data?: T;
  error?: { detail?: string; request_id?: string };
  response: Response;
}): T {
  if (!response.ok || data === undefined)
    throw new ApiError(
      response.status,
      `${error?.detail || "Analytical request failed."}${error?.request_id ? ` Reference: ${error.request_id}` : ""}`,
    );
  return data;
}
export async function getAnalyticalOptions(source_search?: string) {
  return result(
    await api.GET("/api/v1/analytics/options", {
      params: { query: { source_search: source_search || undefined } },
    }),
  );
}
export async function getAnalyticalRuns(cursor?: string) {
  return result(
    await api.GET("/api/v1/analytics/runs", {
      params: { query: { cursor, limit: 10 } },
    }),
  );
}
export async function getAnalyticalRun(id: string) {
  return result(
    await api.GET("/api/v1/analytics/runs/{run_id}", {
      params: { path: { run_id: id } },
    }),
  );
}
export async function getAnalyticalPublic() {
  return result(await api.GET("/api/v1/analytics/public"));
}
export async function getAnalyticalEvidence() {
  return result(await api.GET("/api/v1/analytics/evidence"));
}
export async function requestAnalyticalRun(
  me: Me,
  body: AnalyticalRequest,
  key: string,
) {
  return result(
    await api.POST("/api/v1/analytics/runs", {
      body,
      headers: { "X-CSRF-Token": me.csrf_token },
      params: { header: { "Idempotency-Key": key } },
    }),
  );
}
export type AnalyticalCommand =
  | { action: "gap-drill"; event_id: string; reason: string }
  | { action: "repair" | "retry"; reason: string };
export async function analyticalCommand(
  me: Me,
  run: AnalyticalRun,
  command: AnalyticalCommand,
  key: string,
) {
  const common = {
    headers: { "X-CSRF-Token": me.csrf_token },
    params: { path: { run_id: run.id }, header: { "Idempotency-Key": key } },
  };
  if (command.action === "gap-drill")
    return result(
      await api.POST("/api/v1/analytics/runs/{run_id}/gap-drill", {
        ...common,
        body: { event_id: command.event_id, reason: command.reason },
      }),
    );
  if (command.action === "repair")
    return result(
      await api.POST("/api/v1/analytics/runs/{run_id}/repair", {
        ...common,
        body: { reason: command.reason },
      }),
    );
  return result(
    await api.POST("/api/v1/analytics/runs/{run_id}/retry", {
      ...common,
      params: {
        ...common.params,
        header: { ...common.params.header, "If-Match": run.attempts },
      },
      body: { reason: command.reason },
    }),
  );
}
export async function downloadAnalyticalManifest(run: AnalyticalRun) {
  const response = await fetch(
    `/api/v1/analytics/runs/${encodeURIComponent(run.id)}/manifest`,
    { credentials: "same-origin", cache: "no-store" },
  );
  if (!response.ok)
    throw new ApiError(
      response.status,
      "The manifest could not be verified within your current access.",
    );
  const url = URL.createObjectURL(await response.blob());
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `meridian-analytics-${run.id}.json`;
  anchor.click();
  URL.revokeObjectURL(url);
}
