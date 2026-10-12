import type { components } from "../../../packages/contracts/api";
import { api, ApiError, type Me } from "./api";
export type Batch = components["schemas"]["BatchView"];
export type BatchRequest = components["schemas"]["BatchRequest"];
export type UploadRequest = components["schemas"]["UploadRequest"];
export type ErrorPage = components["schemas"]["ErrorPage"];
export type PreviewPage = components["schemas"]["PreviewPage"];
export type ArtifactKind = components["schemas"]["LinkRequest"]["kind"];

function result<T>({
  data,
  error,
  response,
}: {
  data?: T;
  error?: { detail?: string; request_id?: string };
  response: Response;
}): T {
  if (!response.ok || data === undefined) {
    const message =
      error?.detail || "Data Quality could not complete this request.";
    throw new ApiError(
      response.status,
      `${message}${error?.request_id ? ` Reference: ${error.request_id}` : ""}`,
    );
  }
  return data;
}
function headers(me: Me) {
  return { "X-CSRF-Token": me.csrf_token };
}
export async function getBatches(cursor?: string) {
  return result(
    await api.GET("/api/v1/ingestion/batches", {
      params: { query: { cursor, limit: 10 } },
    }),
  );
}
export async function getBatch(id: string) {
  return result(
    await api.GET("/api/v1/ingestion/batches/{batch_id}", {
      params: { path: { batch_id: id } },
    }),
  );
}
export async function getErrors(id: string, cursor: number, code?: string) {
  return result(
    await api.GET("/api/v1/ingestion/batches/{batch_id}/errors", {
      params: {
        path: { batch_id: id },
        query: { cursor, code: code || undefined, limit: 20 },
      },
    }),
  );
}
export async function getPreview(id: string, cursor: number) {
  return result(
    await api.GET("/api/v1/ingestion/batches/{batch_id}/preview", {
      params: { path: { batch_id: id }, query: { cursor, limit: 20 } },
    }),
  );
}
export function sourceMedia(
  file: Pick<File, "name" | "size">,
): UploadRequest["media_type"] {
  if (file.size <= 0 || file.size > 8 * 1024 * 1024)
    throw new Error("Choose a nonempty file no larger than 8 MiB.");
  const extension = file.name.split(".").at(-1)?.toLowerCase();
  if (extension === "csv") return "text/csv";
  if (extension === "json") return "application/json";
  throw new Error("Choose a workforce schema v1 CSV or JSON file.");
}
export async function registerSource(
  me: Me,
  file: File,
  input: Omit<BatchRequest, "upload_id">,
  progress: (message: string) => void,
) {
  const media = sourceMedia(file);
  progress("Checking source size and fingerprint…");
  const bytes = await file.arrayBuffer();
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  const sha256 = Array.from(new Uint8Array(digest), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
  const intent = result(
    await api.POST("/api/v1/ingestion/uploads", {
      body: { sha256, size_bytes: file.size, media_type: media },
      headers: headers(me),
      params: { header: { "Idempotency-Key": crypto.randomUUID() } },
    }),
  );
  // Only a same-origin upload target may receive the session and CSRF token.
  const target = new URL(intent.upload_url, window.location.origin);
  if (target.origin !== window.location.origin)
    throw new Error("Untrusted source upload target.");
  progress("Uploading to private source storage…");
  const uploaded = await fetch(target, {
    method: "PUT",
    credentials: "same-origin",
    body: bytes,
    headers: { "Content-Type": media, "X-CSRF-Token": me.csrf_token },
  });
  if (!uploaded.ok)
    throw new ApiError(
      uploaded.status,
      "Source upload failed. No batch was registered. Check your session and retry.",
    );
  progress("Registering the verified source for validation…");
  return result(
    await api.POST("/api/v1/ingestion/batches", {
      body: { ...input, upload_id: intent.id },
      headers: headers(me),
      params: { header: { "Idempotency-Key": crypto.randomUUID() } },
    }),
  );
}
export async function publishSource(me: Me, batch: Batch, key: string) {
  return result(
    await api.POST("/api/v1/ingestion/batches/{batch_id}/publish", {
      params: {
        path: { batch_id: batch.id },
        header: { "If-Match": batch.version, "Idempotency-Key": key },
      },
      headers: { "X-CSRF-Token": me.csrf_token },
      body: { confirm_zero_rejects: true },
    }),
  );
}
export async function retrySource(
  me: Me,
  batch: Batch,
  reason: string,
  key: string,
) {
  return result(
    await api.POST("/api/v1/ingestion/batches/{batch_id}/retry", {
      params: {
        path: { batch_id: batch.id },
        header: { "If-Match": batch.version, "Idempotency-Key": key },
      },
      headers: { "X-CSRF-Token": me.csrf_token },
      body: { reason },
    }),
  );
}
export async function downloadSource(me: Me, batch: Batch, kind: ArtifactKind) {
  const link = result(
    await api.POST("/api/v1/ingestion/batches/{batch_id}/download-links", {
      params: {
        path: { batch_id: batch.id },
        header: { "Idempotency-Key": crypto.randomUUID() },
      },
      headers: headers(me),
      body: { kind },
    }),
  );
  const target = new URL(link.url, window.location.origin);
  if (target.origin !== window.location.origin)
    throw new Error("Untrusted artifact target.");
  const response = await fetch(target, {
    credentials: "same-origin",
    cache: "no-store",
  });
  if (!response.ok)
    throw new ApiError(
      response.status,
      "Artifact is unavailable. Refresh your access and source revision, then try again.",
    );
  const url = URL.createObjectURL(await response.blob());
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `meridian-${batch.id}-${kind}.${kind === "errors" ? "csv" : "json"}`;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
export function canPublish(batch: Batch): boolean {
  return (
    batch.state === "VALIDATED" &&
    batch.rejected_count === 0 &&
    batch.accepted_count > 0 &&
    batch.accepted_count === batch.total_count &&
    !batch.pending_job &&
    !batch.failed_job
  );
}
