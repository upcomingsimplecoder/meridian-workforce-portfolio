"use client";
import { Shell } from "@/components/shell";
import { OverviewContent } from "@/components/overview";
export default function Home() {
  return <Shell>{(me) => <OverviewContent me={me} />}</Shell>;
}
