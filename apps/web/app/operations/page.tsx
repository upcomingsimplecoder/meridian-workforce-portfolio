"use client";
import { Shell } from "@/components/shell";
import { OperationsConsole } from "@/components/operations-console";
export default function OperationsPage() {
  return <Shell>{(me) => <OperationsConsole me={me} />}</Shell>;
}
