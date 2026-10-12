import type { components } from "../../../packages/contracts/api";
import { api, ApiError, type Me } from "./api";

export type Replay = components["schemas"]["ReplayView"];
export type ReplayRequest = components["schemas"]["ReplayRequest"];
export type AuditFilters = {
  correlation_id?: string;
  permission?: "ALLOW" | "DENY";
  verb?: string;
};
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
      `${error?.detail || "Evidence request failed."}${error?.request_id ? ` Reference: ${error.request_id}` : ""}`,
    );
  return data;
}
export async function getReplayChoices(
  kind: ReplayRequest["kind"],
  search?: string,
) {
  return result(
    await api.GET("/api/v1/replay/choices", {
      params: { query: { kind, search: search || undefined } },
    }),
  );
}
export async function getReplayList(cursor?: string, subject_id?: string) {
  return result(
    await api.GET("/api/v1/replay/capsules", {
      params: {
        query: { cursor, subject_id: subject_id || undefined, limit: 20 },
      },
    }),
  );
}
export async function getReplay(id: string) {
  return result(
    await api.GET("/api/v1/replay/capsules/{capsule_id}", {
      params: { path: { capsule_id: id } },
    }),
  );
}
export async function captureReplay(me: Me, body: ReplayRequest, key: string) {
  return result(
    await api.POST("/api/v1/replay/capsules", {
      body,
      headers: { "X-CSRF-Token": me.csrf_token },
      params: { header: { "Idempotency-Key": key } },
    }),
  );
}
export async function verifyReplay(
  me: Me,
  replay: Replay,
  reason: string,
  key: string,
) {
  return result(
    await api.POST("/api/v1/replay/capsules/{capsule_id}/verify", {
      body: { reason },
      headers: { "X-CSRF-Token": me.csrf_token },
      params: {
        path: { capsule_id: replay.id },
        header: {
          "Idempotency-Key": key,
          "If-Match": replay.attempts[0].number,
        },
      },
    }),
  );
}
export async function getAudit(filters: AuditFilters, cursor?: string) {
  return result(
    await api.GET("/api/v1/replay/audit", {
      params: { query: { ...filters, cursor, limit: 25 } },
    }),
  );
}
export async function downloadReplay(replay: Replay, attemptId: string) {
  const response = await fetch(
    `/api/v1/replay/capsules/${encodeURIComponent(replay.id)}/attempts/${encodeURIComponent(attemptId)}/export`,
    { credentials: "same-origin", cache: "no-store" },
  );
  if (!response.ok)
    throw new ApiError(
      response.status,
      "The evidence could not be reproduced within your current access. Refresh the attempt history.",
    );
  const url = URL.createObjectURL(await response.blob());
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `meridian-replay-${attemptId}.json`;
  anchor.click();
  URL.revokeObjectURL(url);
}
export function replayHref(kind: ReplayRequest["kind"], subject: string) {
  return `/audit?kind=${kind}&subject=${encodeURIComponent(subject)}`;
}
