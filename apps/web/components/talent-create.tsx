"use client";
import { useState } from "react";
import type { TalentCreate, TalentOptions } from "@/lib/talent";
type Assignment = TalentCreate["assignments"][number];
const empty = (): Assignment => ({
  employee_id: "",
  author_id: "",
  calibrator_id: "",
  hr_signer_id: "",
});
export function TalentCreateForm({
  options,
  pending,
  onCreate,
}: {
  options: TalentOptions;
  pending: boolean;
  onCreate: (body: TalentCreate) => void;
}) {
  const [assignments, setAssignments] = useState<Assignment[]>([empty()]);
  const [error, setError] = useState("");
  const [title, setTitle] = useState("");
  const [snapshot, setSnapshot] = useState("");
  const [policy, setPolicy] = useState("");
  const [asOf, setAsOf] = useState("");
  const [due, setDue] = useState("");
  const [reason, setReason] = useState("");
  function field(index: number, name: keyof Assignment, value: string) {
    setAssignments((rows) =>
      rows.map((r, i) => (i === index ? { ...r, [name]: value } : r)),
    );
  }
  return (
    <form
      className="panel talent-create"
      onSubmit={(e) => {
        e.preventDefault();
        setError("");
        if (
          assignments.some(
            (r) =>
              !/^SYN-[0-9]{6}$/.test(r.employee_id) ||
              new Set([r.author_id, r.calibrator_id, r.hr_signer_id]).size !==
                3,
          ) ||
          new Set(assignments.map((r) => r.employee_id)).size !==
            assignments.length
        ) {
          setError(
            "Use unique synthetic employee IDs and three different participants per packet.",
          );
          return;
        }
        onCreate({
          title,
          snapshot_id: snapshot,
          policy_id: policy,
          as_of: asOf,
          due_date: due,
          assignments,
          reason,
        });
      }}
    >
      <div className="panel-heading">
        <div>
          <h2>Configure a human review</h2>
          <p>
            Freeze source context and three independent assignments before
            launch.
          </p>
        </div>
      </div>
      <fieldset disabled={pending}>
        <div className="talent-form-grid">
          <label>
            Review cycle title
            <input
              required
              minLength={3}
              maxLength={100}
              value={title}
              onChange={(e) => setTitle(e.target.value)}
            />
          </label>
          <label>
            Workforce snapshot
            <select
              aria-label="Workforce snapshot"
              required
              value={snapshot}
              onChange={(e) => setSnapshot(e.target.value)}
            >
              <option value="">Choose a published snapshot</option>
              {options.snapshots.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            Narrative policy
            <select
              aria-label="Narrative policy"
              required
              value={policy}
              onChange={(e) => setPolicy(e.target.value)}
            >
              <option value="">Choose a sealed policy</option>
              {options.policies.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            Context effective date
            <input
              type="date"
              required
              value={asOf}
              onChange={(e) => setAsOf(e.target.value)}
            />
          </label>
          <label>
            Review deadline
            <input
              type="date"
              required
              value={due}
              onChange={(e) => setDue(e.target.value)}
            />
          </label>
        </div>
        <div className="talent-assignments">
          {assignments.map((r, i) => (
            <div className="talent-assignment" key={i}>
              <h3>Packet {i + 1}</h3>
              <label>
                Synthetic employee {i + 1}
                <input
                  required
                  pattern="SYN-[0-9]{6}"
                  value={r.employee_id}
                  onChange={(e) => field(i, "employee_id", e.target.value)}
                />
              </label>
              {(["author_id", "calibrator_id", "hr_signer_id"] as const).map(
                (key, j) => (
                  <label key={key}>
                    {["Author", "Calibrator", "HR signer"][j]} {i + 1}
                    <select
                      aria-label={`${["Author", "Calibrator", "HR signer"][j]} ${i + 1}`}
                      required
                      value={r[key]}
                      onChange={(e) => field(i, key, e.target.value)}
                    >
                      <option value="">Choose participant</option>
                      {[
                        options.authors,
                        options.calibrators,
                        options.hr_signers,
                      ][j].map((person) => (
                        <option key={person.id} value={person.id}>
                          {person.label}
                        </option>
                      ))}
                    </select>
                  </label>
                ),
              )}
              {assignments.length > 1 && (
                <button
                  type="button"
                  className="button"
                  aria-label={`Remove packet ${i + 1}`}
                  onClick={() =>
                    setAssignments((rows) =>
                      rows.filter((_, index) => index !== i),
                    )
                  }
                >
                  Remove
                </button>
              )}
            </div>
          ))}
        </div>
        <button
          type="button"
          className="button"
          disabled={assignments.length >= 500}
          onClick={() => setAssignments((rows) => [...rows, empty()])}
        >
          Add review assignment
        </button>
        <label className="talent-reason">
          Configuration reason
          <textarea
            required
            minLength={3}
            maxLength={240}
            rows={2}
            value={reason}
            onChange={(e) => setReason(e.target.value)}
          />
        </label>
        {error && (
          <p role="alert" className="error-box">
            {error}
          </p>
        )}
        <button className="button primary" type="submit">
          {pending ? "Saving configuration…" : "Save review configuration"}
        </button>
      </fieldset>
    </form>
  );
}
