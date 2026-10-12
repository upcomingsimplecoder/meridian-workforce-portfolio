"use client";
import { Suspense } from "react";
import { useSearchParams } from "next/navigation";
import { Shell } from "@/components/shell";
import { WorkforceContent } from "@/components/workforce";
function LinkedWorkforce() {
  const params = useSearchParams();
  const initial = {
    snapshot: params.get("snapshot_id") || undefined,
    asOf: params.get("as_of") || undefined,
  };
  return (
    <Shell>
      {(me) => (
        <WorkforceContent
          key={`${initial.snapshot}:${initial.asOf}`}
          me={me}
          initial={initial}
        />
      )}
    </Shell>
  );
}
export default function WorkforcePage() {
  return (
    <Suspense
      fallback={
        <main className="center-state" role="status">
          Loading source context…
        </main>
      }
    >
      <LinkedWorkforce />
    </Suspense>
  );
}
