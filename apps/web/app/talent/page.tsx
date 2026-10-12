"use client";
import { Shell } from "@/components/shell";
import { TalentStudio } from "@/components/talent-studio";
import "../talent.css";
export default function TalentPage() {
  return <Shell>{(me) => <TalentStudio key={me.id} me={me} />}</Shell>;
}
