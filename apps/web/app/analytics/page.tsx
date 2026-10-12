"use client";
import { Shell } from "@/components/shell";
import { AnalyticsConsole } from "@/components/analytics-console";
import "../analytics.css";
export default function AnalyticsPage() {
  return <Shell>{(me) => <AnalyticsConsole key={me.id} me={me} />}</Shell>;
}
