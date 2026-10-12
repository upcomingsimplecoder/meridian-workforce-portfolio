"use client";
import { Suspense } from "react";
import { Shell } from "@/components/shell";
import { ReplayConsole } from "@/components/replay-console";
import "../replay.css";
export default function AuditPage() {
  return (
    <Shell>
      {(me) => (
        <Suspense fallback={<p role="status">Opening persisted evidence…</p>}>
          <ReplayConsole key={me.id} me={me} />
        </Suspense>
      )}
    </Shell>
  );
}
