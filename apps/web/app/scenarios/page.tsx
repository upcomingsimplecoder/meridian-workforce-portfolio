"use client";
import { Shell } from "@/components/shell";
import { ScenarioLab } from "@/components/scenario-lab";
export default function ScenariosPage() {
  return <Shell>{(me) => <ScenarioLab key={me.id} me={me} />}</Shell>;
}
