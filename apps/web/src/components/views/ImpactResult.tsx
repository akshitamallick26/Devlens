"use client";

import { useMemo, useState } from "react";
import type { ImpactedSymbol, ImpactResponse } from "@/lib/api/types";
import { formatCount, lineRange } from "@/lib/format";
import { DepthRingGraph } from "@/components/graph/DepthRingGraph";
import {
  Badge,
  EmptyState,
  KeyValue,
  Notice,
  Section,
  ui,
} from "@/components/ui/primitives";
import {
  type Selection,
  useSelection,
} from "@/components/workstation/WorkspaceContext";
import styles from "./views.module.css";

const PAGE_SIZE = 100;

function impactedSelection(symbol: ImpactedSymbol): Selection {
  return {
    kind: "symbol",
    title: symbol.qualified_name,
    subtitle: symbol.file_path,
    symbolId: symbol.symbol_id,
    fileId: symbol.file_id,
    facts: [
      ["kind", symbol.symbol_kind],
      ["depth from target", String(symbol.depth)],
      ["reached via", symbol.relationship_types.join(", ") || "not recorded"],
    ],
  };
}

/** Bounded table: renders PAGE_SIZE rows at a time, never the whole set at once. */
function ImpactTable({
  rows,
  caption,
}: {
  rows: ImpactedSymbol[];
  caption: string;
}) {
  const { select } = useSelection();
  const [limit, setLimit] = useState(PAGE_SIZE);
  if (rows.length === 0)
    return <EmptyState title={`No ${caption.toLowerCase()}`} />;
  return (
    <>
      <table className={ui.table}>
        <caption className="visually-hidden">{caption}</caption>
        <thead>
          <tr>
            <th scope="col">Symbol</th>
            <th scope="col">Via</th>
            <th scope="col" className={ui.numeric}>
              Depth
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, limit).map((row) => (
            <tr key={row.symbol_id}>
              <td>
                <button
                  type="button"
                  className={ui.rowButton}
                  onClick={() => select(impactedSelection(row))}
                >
                  <span className={ui.symbolName}>{row.qualified_name}</span>
                  <span className={ui.path}> {row.file_path}</span>
                </button>
              </td>
              <td>
                {row.relationship_types.map((rel) => (
                  <span key={rel} className={ui.tag} style={{ marginRight: 4 }}>
                    {rel}
                  </span>
                ))}
              </td>
              <td className={ui.numeric}>{row.depth}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length > limit ? (
        <div style={{ padding: "6px 12px" }}>
          <button
            type="button"
            className={ui.button}
            onClick={() => setLimit((l) => l + PAGE_SIZE)}
          >
            Show {Math.min(PAGE_SIZE, rows.length - limit)} more of{" "}
            {rows.length - limit}
          </button>
        </div>
      ) : null}
    </>
  );
}

export function ImpactExplanationBlock({ result }: { result: ImpactResponse }) {
  const { select } = useSelection();
  if (result.analysis_status === "deterministic") return null;
  if (result.analysis_status === "deterministic_only" || !result.explanation) {
    return (
      <Notice>
        An explanation was requested but is unavailable (no AI provider
        configured, or the provider failed). The deterministic facts above are
        complete without it.
      </Notice>
    );
  }
  const { explanation, evidence } = result;
  return (
    <section
      className={styles.explanation}
      aria-labelledby="impact-explanation"
    >
      <div>
        <span className={styles.explanationLabel} id="impact-explanation">
          AI explanation
        </span>{" "}
        <span className={ui.faint}>
          Written from the facts above. It did not discover dependencies;
          citations point to the evidence it was given.
        </span>
      </div>
      <p>{explanation.summary}</p>
      <div>
        <h3>Why it matters</h3>
        <p>{explanation.why_it_matters}</p>
      </div>
      {(
        [
          ["Affected areas", explanation.affected_areas],
          ["Review recommendations", explanation.review_recommendations],
          ["Test recommendations", explanation.test_recommendations],
        ] as const
      ).map(([heading, items]) =>
        items.length ? (
          <div key={heading}>
            <h3>{heading}</h3>
            <ul className={styles.bullets}>
              {items.map((item, i) => (
                <li key={i}>{item}</li>
              ))}
            </ul>
          </div>
        ) : null,
      )}
      {explanation.evidence.length ? (
        <div>
          <h3>Cited evidence</h3>
          <ul className={styles.list}>
            {explanation.evidence.map((id) => {
              const location = evidence?.[id];
              return (
                <li key={id}>
                  {location ? (
                    <button
                      type="button"
                      className={ui.rowButton}
                      onClick={() =>
                        select({
                          kind: "evidence",
                          title: `${id}: ${location.file_path}${lineRange(location.line_start, location.line_end)}`,
                          facts: [["evidence id", id]],
                        })
                      }
                    >
                      <span className={styles.evidenceId}>{id}</span>{" "}
                      <span className={ui.path}>
                        {location.file_path}
                        {lineRange(location.line_start, location.line_end)}
                      </span>
                    </button>
                  ) : (
                    <>
                      <span className={styles.evidenceId}>{id}</span>{" "}
                      <span className={ui.faint}>
                        location not resolved by the backend
                      </span>
                    </>
                  )}
                </li>
              );
            })}
          </ul>
        </div>
      ) : null}
      <p className={ui.faint}>Limitations: {explanation.limitations}</p>
    </section>
  );
}

export function ImpactResult({ result }: { result: ImpactResponse }) {
  const { selection, select } = useSelection();
  const all = useMemo(
    () => [...result.direct_impacts, ...result.transitive_impacts],
    [result.direct_impacts, result.transitive_impacts],
  );
  const byId = useMemo(() => new Map(all.map((s) => [s.symbol_id, s])), [all]);
  const ringNodes = useMemo(
    () =>
      all.map((s) => ({
        id: s.symbol_id,
        label: s.qualified_name,
        depth: s.depth,
        detail: s.relationship_types.join(", "),
      })),
    [all],
  );

  return (
    <div className={styles.stack}>
      <div>
        <span className={styles.factsLabel}>Deterministic facts</span>{" "}
        <span className={ui.faint}>
          Reverse traversal of the persisted dependency graph for this revision.
        </span>
      </div>
      <div className={styles.metrics}>
        <div className={styles.metric}>
          <span className={styles.metricLabel}>target</span>
          <span className={ui.symbolName}>{result.target_qualified_name}</span>
        </div>
        <div className={styles.metric}>
          <span className={styles.metricLabel}>direct dependents</span>
          <span className={styles.metricValue}>
            {formatCount(result.direct_impacts.length)}
          </span>
        </div>
        <div className={styles.metric}>
          <span className={styles.metricLabel}>transitive dependents</span>
          <span className={styles.metricValue}>
            {formatCount(result.transitive_impacts.length)}
          </span>
        </div>
        <div className={styles.metric}>
          <span className={styles.metricLabel}>impacted files</span>
          <span className={styles.metricValue}>
            {formatCount(result.impacted_files.length)}
          </span>
        </div>
        <div className={styles.metric}>
          <span className={styles.metricLabel}>max depth</span>
          <span className={styles.metricValue}>{result.max_depth}</span>
        </div>
      </div>

      {all.length === 0 ? (
        <EmptyState title="Nothing depends on this symbol">
          No symbol in this revision has a recorded call, import, inheritance or
          reference edge to it. External callers (outside the repository or via
          dynamic dispatch) are not in the graph.
        </EmptyState>
      ) : (
        <>
          <DepthRingGraph
            label={`Dependents of ${result.target_qualified_name}`}
            target={{
              id: result.target_symbol_id,
              label: result.target_qualified_name,
            }}
            nodes={ringNodes}
            selectedId={selection?.symbolId ?? null}
            onSelect={(id) => {
              const symbol = byId.get(id);
              if (symbol) select(impactedSelection(symbol));
            }}
          />
          <div className={styles.grid2}>
            <Section
              title={`Direct dependents (${result.direct_impacts.length})`}
              flush
            >
              <ImpactTable
                rows={result.direct_impacts}
                caption="Direct dependents"
              />
            </Section>
            <Section
              title={`Transitive dependents (${result.transitive_impacts.length})`}
              flush
            >
              <ImpactTable
                rows={result.transitive_impacts}
                caption="Transitive dependents"
              />
            </Section>
          </div>
          <Section
            title={`Impacted files (${result.impacted_files.length})`}
            flush
          >
            <table className={ui.table}>
              <thead>
                <tr>
                  <th scope="col">File</th>
                  <th scope="col" className={ui.numeric}>
                    Symbols
                  </th>
                  <th scope="col" className={ui.numeric}>
                    Nearest depth
                  </th>
                </tr>
              </thead>
              <tbody>
                {result.impacted_files.slice(0, 200).map((file) => (
                  <tr key={file.file_id}>
                    <td className={ui.path}>{file.path}</td>
                    <td className={ui.numeric}>{file.symbol_count}</td>
                    <td className={ui.numeric}>{file.minimum_depth}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {result.impacted_files.length > 200 ? (
              <p className={ui.faint} style={{ padding: "6px 12px" }}>
                {result.impacted_files.length - 200} more files not shown.
              </p>
            ) : null}
          </Section>
        </>
      )}

      <KeyValue
        items={[
          ["analysis run", result.analysis_run_id ?? "not recorded"],
          [
            "analysis status",
            <Badge
              key="s"
              tone={result.analysis_status === "explained" ? "info" : "accent"}
            >
              {result.analysis_status}
            </Badge>,
          ],
        ]}
      />
      <p className={ui.faint}>
        Evidence paths between dependents are not returned by the impact API;
        each dependent carries its shortest depth and the relationship types
        that reached it.
      </p>

      <ImpactExplanationBlock result={result} />
    </div>
  );
}
