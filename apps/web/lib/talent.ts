import type { components } from "../../../packages/contracts/api";
import { api, ApiError, type Me } from "./api";
export type TalentCycle = components["schemas"]["TalentCycleView"];
export type TalentPacket = components["schemas"]["PacketView"];
export type TalentReview = components["schemas"]["StructuredReview"];
export type TalentOptions = components["schemas"]["TalentOptions"];
export type TalentCreate = components["schemas"]["TalentCreate"];
export type TalentReason = components["schemas"]["TalentReason"];
export type TalentSave = components["schemas"]["TalentSave"];
export type TalentSignature = components["schemas"]["TalentSignature"];
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
      `${error?.detail || "Review request failed."}${error?.request_id ? ` Reference: ${error.request_id}` : ""}`,
    );
  return data;
}
export async function getTalentOptions(search = "") {
  return result(
    await api.GET("/api/v1/talent/options", {
      params: { query: { source_search: search || undefined } },
    }),
  );
}
export async function getTalentCycles(cursor?: string, search?: string) {
  return result(
    await api.GET("/api/v1/talent/cycles", {
      params: { query: { cursor, search: search || undefined, limit: 10 } },
    }),
  );
}
export async function getTalentCycle(id: string) {
  return result(
    await api.GET("/api/v1/talent/cycles/{cycle_id}", {
      params: { path: { cycle_id: id } },
    }),
  );
}
export async function getTalentPackets(
  id: string,
  cursor?: string,
  search?: string,
) {
  return result(
    await api.GET("/api/v1/talent/cycles/{cycle_id}/packets", {
      params: {
        path: { cycle_id: id },
        query: { cursor, search: search || undefined, limit: 25 },
      },
    }),
  );
}
export async function getTalentPacket(
  id: string,
  query?: {
    revision?: number;
    comment_cursor?: string;
    decision_cursor?: number;
  },
) {
  return result(
    await api.GET("/api/v1/talent/packets/{packet_id}", {
      params: { path: { packet_id: id }, query },
    }),
  );
}
export async function getTalentEvidence(cursor?: string) {
  return result(
    await api.GET("/api/v1/talent/evidence", {
      params: { query: { cursor, limit: 25 } },
    }),
  );
}
export async function getTalentExport(id: string, lock_number?: number) {
  return result(
    await api.GET("/api/v1/talent/cycles/{cycle_id}/export", {
      params: { path: { cycle_id: id }, query: { lock_number } },
    }),
  );
}
export async function createTalentCycle(
  me: Me,
  body: TalentCreate,
  key: string,
) {
  return result(
    await api.POST("/api/v1/talent/cycles", {
      body,
      headers: { "X-CSRF-Token": me.csrf_token },
      params: { header: { "Idempotency-Key": key } },
    }),
  );
}
export type TalentCycleCommand =
  | { action: "launch" | "lock" | "retry-export"; body: TalentReason }
  | { action: "reopen"; body: components["schemas"]["TalentReopen"] };
export async function talentCycleCommand(
  me: Me,
  cycle: TalentCycle,
  command: TalentCycleCommand,
  key: string,
) {
  const options = {
    headers: { "X-CSRF-Token": me.csrf_token },
    params: {
      path: { cycle_id: cycle.id },
      header: { "Idempotency-Key": key, "If-Match": cycle.version },
    },
  };
  switch (command.action) {
    case "launch":
      return result(
        await api.POST("/api/v1/talent/cycles/{cycle_id}/launch", {
          ...options,
          body: command.body,
        }),
      );
    case "lock":
      return result(
        await api.POST("/api/v1/talent/cycles/{cycle_id}/lock", {
          ...options,
          body: command.body,
        }),
      );
    case "retry-export":
      return result(
        await api.POST("/api/v1/talent/cycles/{cycle_id}/retry-export", {
          ...options,
          body: command.body,
        }),
      );
    case "reopen":
      return result(
        await api.POST("/api/v1/talent/cycles/{cycle_id}/reopen", {
          ...options,
          body: command.body,
        }),
      );
  }
}
export type TalentPacketCommand =
  | { action: "review" | "calibration"; body: TalentSave }
  | { action: "submit" | "start-calibration" | "return"; body: TalentReason }
  | { action: "ack-calibration" | "approve"; body: TalentSignature }
  | { action: "comments"; body: components["schemas"]["TalentCommentRequest"] };
export async function talentPacketCommand(
  me: Me,
  packet: TalentPacket,
  command: TalentPacketCommand,
  key: string,
) {
  const options = {
    headers: { "X-CSRF-Token": me.csrf_token },
    params: {
      path: { packet_id: packet.id },
      header: { "Idempotency-Key": key, "If-Match": packet.version },
    },
  };
  switch (command.action) {
    case "review":
      return result(
        await api.POST("/api/v1/talent/packets/{packet_id}/review", {
          ...options,
          body: command.body,
        }),
      );
    case "calibration":
      return result(
        await api.POST("/api/v1/talent/packets/{packet_id}/calibration", {
          ...options,
          body: command.body,
        }),
      );
    case "submit":
      return result(
        await api.POST("/api/v1/talent/packets/{packet_id}/submit", {
          ...options,
          body: command.body,
        }),
      );
    case "start-calibration":
      return result(
        await api.POST("/api/v1/talent/packets/{packet_id}/start-calibration", {
          ...options,
          body: command.body,
        }),
      );
    case "ack-calibration":
      return result(
        await api.POST("/api/v1/talent/packets/{packet_id}/ack-calibration", {
          ...options,
          body: command.body,
        }),
      );
    case "approve":
      return result(
        await api.POST("/api/v1/talent/packets/{packet_id}/approve", {
          ...options,
          body: command.body,
        }),
      );
    case "return":
      return result(
        await api.POST("/api/v1/talent/packets/{packet_id}/return", {
          ...options,
          body: command.body,
        }),
      );
    case "comments":
      return result(
        await api.POST("/api/v1/talent/packets/{packet_id}/comments", {
          ...options,
          body: command.body,
        }),
      );
  }
}
export async function downloadTalentExport(id: string, number: number) {
  const manifest = await getTalentExport(id, number);
  const url = new URL(manifest.download_path, window.location.origin);
  if (
    url.origin !== window.location.origin ||
    url.pathname !== `/api/v1/talent/cycles/${id}/download`
  )
    throw new Error("Review download path is unavailable.");
  const response = await fetch(url, { credentials: "same-origin" });
  if (!response.ok)
    throw new ApiError(
      response.status,
      "The review export could not be downloaded with your current access.",
    );
  const href = URL.createObjectURL(await response.blob());
  const link = document.createElement("a");
  link.href = href;
  link.download = `meridian-talent-${id.slice(0, 8)}-v${number}.json`;
  link.click();
  URL.revokeObjectURL(href);
}
