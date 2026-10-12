"use client";
import { useState } from "react";

export function SnapshotSearch({
  onSearch,
  pending = false,
}: {
  onSearch: (query: string) => void;
  pending?: boolean;
}) {
  const [draft, setDraft] = useState("");
  return (
    <form
      className="snapshot-search"
      onSubmit={(event) => {
        event.preventDefault();
        onSearch(draft.trim());
      }}
    >
      <label>
        Search published sources
        <input
          type="search"
          maxLength={100}
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          placeholder="Find a historical snapshot by title"
        />
      </label>
      <button className="button" disabled={pending}>
        {pending ? "Finding source…" : "Find source"}
      </button>
    </form>
  );
}
