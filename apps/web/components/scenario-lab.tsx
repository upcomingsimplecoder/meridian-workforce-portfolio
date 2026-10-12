"use client";
import { SnapshotSearch } from "./snapshot-search";
import { useState } from "react";
import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import type { Me } from "@/lib/api";
import {
  compareScenarios,
  createScenario,
  getScenarioOptions,
  getScenarios,
  money,
  runCommand,
  type Command,
  type Scenario,
  type ScenarioRequest,
} from "@/lib/scenarios";
import { ScenarioCreate } from "./scenario-create";
import { ScenarioDetail } from "./scenario-detail";

export function ScenarioLab({ me }: { me: Me }) {
  const [selected, setSelected] = useState<string>();
  const [create, setCreate] = useState(false);
  const [search, setSearch] = useState("");
  const [sourceSearch, setSourceSearch] = useState("");
  const [cursors, setCursors] = useState<(string | undefined)[]>([undefined]);
  const [compareIds, setCompareIds] = useState<string[]>([]);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const client = useQueryClient();
  const permitted = me.capabilities.includes("planning.write");
  const options = useQuery({
    queryKey: ["scenarios", me.id, "options", sourceSearch],
    queryFn: () => getScenarioOptions(sourceSearch),
    placeholderData: keepPreviousData,
    enabled: permitted,
  });
  const list = useQuery({
    queryKey: ["scenarios", me.id, "list", cursors.at(-1), search],
    queryFn: () => getScenarios(cursors.at(-1), search),
    enabled: permitted,
  });
  const comparison = useQuery({
    queryKey: ["scenarios", me.id, "compare", compareIds],
    queryFn: () => compareScenarios(compareIds),
    enabled: permitted && compareIds.length >= 2,
  });
  const mutation = useMutation({
    mutationFn: (
      task:
        | { create: ScenarioRequest; key: string }
        | { scenario: Scenario; command: Command; key: string },
    ) =>
      "create" in task
        ? createScenario(me, task.create, task.key)
        : runCommand(me, task.scenario, task.command, task.key),
    onMutate: () => {
      setError("");
      setMessage("");
    },
    onSuccess: async (row) => {
      setSelected(row.id);
      setCreate(false);
      setMessage(
        `Saved ${row.title} · revision ${row.revision} · ${row.state}.`,
      );
      client.setQueryData(["scenarios", me.id, "detail", row.id], row);
      await client.invalidateQueries({ queryKey: ["scenarios", me.id] });
    },
    onError: async (err) => {
      setError(err.message);
      await client.invalidateQueries({ queryKey: ["scenarios", me.id] });
    },
  });
  if (!permitted)
    return (
      <>
        <div className="page-heading">
          <div>
            <span className="eyebrow">GOVERNED PLANNING</span>
            <h1>Scenario Lab</h1>
          </div>
        </div>
        <section className="panel empty-state">
          <h2>Planning access is restricted.</h2>
          <p>
            Your current grants do not allow access to scenario inputs or
            forecasts.
          </p>
        </section>
      </>
    );
  const failed = options.error || list.error;
  return (
    <>
      <div className="page-heading">
        <div>
          <span className="eyebrow">PLAN WITH PROVENANCE</span>
          <h1>Scenario Lab</h1>
          <p>Branch the workforce. Model the cost. Preserve the decisions.</p>
        </div>
        <button
          className="button primary"
          disabled={!options.data || mutation.isPending}
          onClick={() => setCreate((prev) => !prev)}
        >
          {create ? "Close new plan" : "New plan"}
        </button>
      </div>
      {error && (
        <div role="alert" className="error-box">
          {error}
          <button
            className="button"
            onClick={() =>
              client.invalidateQueries({ queryKey: ["scenarios", me.id] })
            }
          >
            Refresh current state
          </button>
        </div>
      )}
      {message && (
        <p role="status" className="scenario-notice">
          {message}
        </p>
      )}
      {failed ? (
        <div role="alert" className="error-box">
          {failed.message}
          <button
            className="button"
            onClick={() => {
              options.refetch();
              list.refetch();
            }}
          >
            Retry connection
          </button>
        </div>
      ) : options.isPending || list.isPending ? (
        <p role="status">Loading authorized plans and assumptions…</p>
      ) : (
        <>
          {create && (
            <>
              <SnapshotSearch
                onSearch={setSourceSearch}
                pending={options.isFetching}
              />
              <ScenarioCreate
                options={options.data}
                pending={mutation.isPending}
                onCreate={(body) =>
                  mutation.mutate({ create: body, key: crypto.randomUUID() })
                }
              />
            </>
          )}
          <section
            className="panel scenario-list"
            aria-label="Authorized plans"
            aria-busy={mutation.isPending}
          >
            <div className="panel-heading">
              <h2>Your plans and assigned reviews</h2>
              <form
                onSubmit={(event) => {
                  event.preventDefault();
                  const data = new FormData(event.currentTarget);
                  setSearch(String(data.get("search")));
                  setCursors([undefined]);
                }}
              >
                <label className="sr-only" htmlFor="plan-search">
                  Search plan titles
                </label>
                <input
                  id="plan-search"
                  name="search"
                  type="search"
                  maxLength={100}
                  placeholder="Search plans"
                />
                <button className="button">Search</button>
              </form>
            </div>
            {list.data.items.length ? (
              <div
                className="table-scroll"
                tabIndex={0}
                role="region"
                aria-label="Plan table"
              >
                <table className="data-table">
                  <caption>
                    Server-scoped plans · select two or three to compare
                  </caption>
                  <thead>
                    <tr>
                      <th scope="col">Compare</th>
                      <th scope="col">Plan</th>
                      <th scope="col">Period / scope</th>
                      <th scope="col">Revision</th>
                      <th scope="col">State</th>
                      <th scope="col">Calculated annual cost</th>
                    </tr>
                  </thead>
                  <tbody>
                    {list.data.items.map((row) => (
                      <tr key={row.id} aria-selected={row.id === selected}>
                        <td>
                          <input
                            type="checkbox"
                            aria-label={`Compare ${row.title}`}
                            checked={compareIds.includes(row.id)}
                            disabled={
                              !compareIds.includes(row.id) &&
                              compareIds.length >= 3
                            }
                            onChange={(event) =>
                              setCompareIds((prev) =>
                                event.target.checked
                                  ? [...prev, row.id]
                                  : prev.filter((id) => id !== row.id),
                              )
                            }
                          />
                        </td>
                        <th scope="row">
                          <button
                            className="text-button"
                            disabled={mutation.isPending}
                            aria-pressed={row.id === selected}
                            onClick={() => {
                              setSelected(row.id);
                              setMessage("");
                            }}
                          >
                            {row.title}
                          </button>
                        </th>
                        <td>
                          {row.year} · {row.scope_orgs.join(", ")}
                        </td>
                        <td>{row.revision}</td>
                        <td>{row.state}</td>
                        <td>{money(row.annual_total_minor, row)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <div className="empty-state">
                <h3>
                  {search
                    ? "No matching plans."
                    : "Your first plan starts with a pinned source."}
                </h3>
                <p>
                  {search
                    ? "Try another title."
                    : "Create a plan with a published synthetic snapshot and a separate reviewer."}
                </p>
              </div>
            )}
            {mutation.isPending && (
              <p role="status">
                Saving the plan and refreshing the authorized list…
              </p>
            )}
            <div className="scenario-pagination">
              <button
                className="button"
                disabled={cursors.length === 1}
                onClick={() => setCursors((prev) => prev.slice(0, -1))}
              >
                Previous plans
              </button>
              <span>Page {cursors.length}</span>
              <button
                className="button"
                disabled={!list.data.next_cursor}
                onClick={() =>
                  setCursors((prev) => [
                    ...prev,
                    list.data.next_cursor || undefined,
                  ])
                }
              >
                Next plans
              </button>
            </div>
          </section>
          {compareIds.length >= 2 && (
            <section
              className="panel scenario-comparison"
              aria-label="Branch comparison"
            >
              <div className="panel-heading">
                <h2>Branch comparison</h2>
                <button
                  className="text-button"
                  onClick={() => setCompareIds([])}
                >
                  Clear comparison
                </button>
              </div>
              {comparison.isPending ? (
                <p role="status">Loading versioned comparison…</p>
              ) : comparison.error ? (
                <p role="alert" className="error-box">
                  {comparison.error.message}
                </p>
              ) : (
                <div
                  className="table-scroll"
                  tabIndex={0}
                  role="region"
                  aria-label="Comparison table"
                >
                  <table className="data-table">
                    <caption>
                      Server calculated changes from the first selected plan
                    </caption>
                    <thead>
                      <tr>
                        <th scope="col">Plan</th>
                        <th scope="col">Annual cost</th>
                        <th scope="col">Annual change</th>
                        <th scope="col">Closing headcount</th>
                      </tr>
                    </thead>
                    <tbody>
                      {comparison.data?.items.map((item) => (
                        <tr key={item.scenario.id}>
                          <th scope="row">{item.scenario.title}</th>
                          <td>
                            {money(item.result.annual.total_minor, item.result)}
                          </td>
                          <td>{money(item.annual_delta_minor, item.result)}</td>
                          <td>
                            {item.result.months.at(-1)?.closing_headcount}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </section>
          )}
          {selected && (
            <ScenarioDetail
              key={selected}
              id={selected}
              me={me}
              options={options.data}
              pending={mutation.isPending}
              onCommand={(scenario, command) =>
                mutation.mutate({ scenario, command, key: crypto.randomUUID() })
              }
            />
          )}
        </>
      )}
    </>
  );
}
