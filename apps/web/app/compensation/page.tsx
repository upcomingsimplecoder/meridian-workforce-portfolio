"use client";
import { Shell } from "@/components/shell";
import { CompensationStudio } from "@/components/compensation-studio";
import "../compensation.css";
export default function CompensationPage() {
  return <Shell>{(me) => <CompensationStudio key={me.id} me={me} />}</Shell>;
}
