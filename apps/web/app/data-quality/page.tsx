"use client";
import { Shell } from "@/components/shell";
import { IngestionConsole } from "@/components/ingestion-console";
export default function DataQualityPage() {
  return <Shell>{(me) => <IngestionConsole me={me} />}</Shell>;
}
