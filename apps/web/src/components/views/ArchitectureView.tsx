"use client";

import { type FormEvent, useMemo, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { getGraphNeighborhood, searchSymbols } from "@/lib/api/endpoints";
import type {
  GraphDirection,
  GraphNeighborhood,
  GraphNode,
  RepositoryOverview,
} from "@/lib/api/types";
import { formatCount, isUuid } from "@/lib/format";
import { useAction, useResource } from "@/lib/useApi";
import { NeighborhoodGraph } from "@/components/graph/NeighborhoodGraph";
import {
  Distribution,
  EmptyState,
  ErrorState,
  Loading,
  Notice,
  Section,
  ui,
} from "@/components/ui/primitives";
import {
  type Selection,
  useSelection,
  useWorkspace,
  WithOverview,
} from "@/components/workstation/WorkspaceContext";
import styles from "./views.module.css";

const DIRECTIONS: { value: GraphDirection; label: string }[] = [
  { value: "both", label: "Dependents and dependencies" },
  { value: "dependents", label: "Dependents (who depends on it)" },
  { value: "dependencies", label: "Dependencies (what it depends on)" },
];
const LIMITS = [25, 50, 100, 200];
const MAX_DEPTH = 4;

export function graphNodeSelection(
  node: GraphNode,
  graph: GraphNeighborhood,
): Selection {
  const outgoing = graph.edges.filter((e) => e.source === node.symbol_id);
  const incoming = graph.edges.filter((e) => e.target === node.symbol_id);
  return {
    kind: "symbol",
    title: node.qualified_name,
    subtitle: node.file_path,
    symbolId: node.symbol_id,
    fileId: node.file_id,
    graphFocus: { direction: graph.direction, depth: graph.depth },
    facts: [
      ["kind", node.kind],
      [
        "position",
        node.reached_via === "focus"
          ? "graph focus"
          : `${node.reached_via} at depth ${node.depth}`,
      ],
      ["edges shown out (dependencies)", String(outgoing.length)],
      ["edges shown in (dependents)", String(incoming.length)],
    ],
  };
}

function SymbolPicker({
  revisionId,
  onPick,
}: {
  revisionId: string;
  onPick: (id: string) => void;
}) {
  const { repositoryId } = useWorkspace();
  const [query, setQuery] = useState("");
  const search = useAction((signal, q: string) =>
    searchSymbols(repositoryId, revisionId, q, signal),
  );

  function submit(event: FormEvent) {
    event.preventDefault();
    const q = query.trim();
    if (!q) return;
    if (isUuid(q)) onPick(q);
    else search.run(q);
  }

  return (
    <div className={styles.stack}>
      <form className={styles.toolbar} onSubmit={submit}>
        <label className={`${ui.label} ${styles.grow}`}>
          Focus symbol (name fragment or symbol id)
          <input
            className={ui.input}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="billing.ledger.post_entry"
            spellCheck={false}
          />
        </label>
        <button type="submit" className={ui.button} disabled={!query.trim()}>
          Find
        </button>
      </form>
      {search.status === "loading" ? (
        <Loading label="Searching symbols" />
      ) : null}
      {search.status === "error" ? <ErrorState error={search.error} /> : null}
      {search.status === "success" ? (
        search.data.symbols.length === 0 ? (
          <p className={ui.muted}>No symbol in this revision matches.</p>
        ) : (
          <ul className={styles.list} aria-label="Matching symbols">
            {search.data.symbols.map((symbol) => (
              <li key={symbol.symbol_id} className={styles.listItem}>
                <button
                  type="button"
                  className={ui.rowButton}
                  onClick={() => {
                    onPick(symbol.symbol_id);
                    search.reset();
                  }}
                >
                  <span className={ui.symbolName}>{symbol.qualified_name}</span>{" "}
                  <span className={ui.tag}>{symbol.kind}</span>{" "}
                  <span className={ui.path}>{symbol.file_path}</span>
                </button>
              </li>
            ))}
            {search.data.truncated ? (
              <li className={ui.faint}>
                More matches exist; refine the query.
              </li>
            ) : null}
          </ul>
        )
      ) : null}
    </div>
  );
}

/** Accessible alternative to the canvas: the same nodes and edges as text. */
function EdgeList({ graph }: { graph: GraphNeighborhood }) {
  const names = useMemo(
    () => new Map(graph.nodes.map((n) => [n.symbol_id, n.qualified_name])),
    [graph.nodes],
  );
  return (
    <details>
      <summary className={ui.muted}>
        {graph.nodes.length} symbols, {graph.edges.length} edges as a list
      </summary>
      <table className={ui.table}>
        <thead>
          <tr>
            <th scope="col">Dependent (source)</th>
            <th scope="col">Relationship</th>
            <th scope="col">Dependency (target)</th>
          </tr>
        </thead>
        <tbody>
          {graph.edges.map((edge) => (
            <tr key={`${edge.source}-${edge.target}-${edge.relationship_type}`}>
              <td className={ui.symbolName}>{names.get(edge.source)}</td>
              <td>
                <span className={ui.tag}>{edge.relationship_type}</span>
              </td>
              <td className={ui.symbolName}>{names.get(edge.target)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </details>
  );
}

function GraphExplorer({
  revisionId,
  focusId,
  direction,
  depth,
  limit,
}: {
  revisionId: string;
  focusId: string;
  direction: GraphDirection;
  depth: number;
  limit: number;
}) {
  const { repositoryId } = useWorkspace();
  const { selection, select } = useSelection();
  const graph = useResource(
    `${revisionId}:${focusId}:${direction}:${depth}:${limit}`,
    (signal) =>
      getGraphNeighborhood(
        repositoryId,
        revisionId,
        { symbolId: focusId, direction, depth, limit },
        signal,
      ),
  );

  if (graph.status === "loading" || graph.status === "idle") {
    return <Loading label="Loading graph neighbourhood" />;
  }
  if (graph.status === "error") {
    return (
      <ErrorState
        error={graph.error}
        onRetry={graph.reload}
        context={
          graph.error.kind === "not_found"
            ? "The focus symbol does not exist in this revision. Symbols are revision-scoped."
            : undefined
        }
      />
    );
  }
  const data = graph.data;
  return (
    <div className={styles.stack}>
      {data.nodes.length <= 1 ? (
        <EmptyState title="No internal edges around this symbol">
          Nothing in this revision depends on it, and it depends on nothing
          inside the repository
          {data.focus_external_references.length
            ? " (it does reference external names, listed below)"
            : ""}
          .
        </EmptyState>
      ) : (
        <NeighborhoodGraph
          graph={data}
          selectedId={selection?.symbolId ?? null}
          onSelect={(node) => select(graphNodeSelection(node, data))}
        />
      )}
      <p className={ui.faint}>
        {formatCount(data.nodes.length)} symbols and{" "}
        {formatCount(data.edges.length)} edges, depth reached{" "}
        {data.max_depth_reached} of {data.depth}.
      </p>
      {data.truncated ? (
        <Notice tone="info">
          Truncated at {data.node_limit} symbols (or the per-hop edge cap).
          Raise the node limit or reduce the depth.
        </Notice>
      ) : null}
      {data.focus_external_references.length ? (
        <p className={ui.muted}>
          External references from the focus (not graph nodes):{" "}
          {data.focus_external_references.map((ref) => (
            <span key={ref} className={ui.tag} style={{ marginRight: 4 }}>
              {ref}
            </span>
          ))}
        </p>
      ) : null}
      <EdgeList graph={data} />
    </div>
  );
}

function Architecture({ overview }: { overview: RepositoryOverview }) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const { graph, important_components: components } = overview;
  const graphStatus = overview.analysis_statuses.find(
    (s) => s.analysis_type === "graph_build",
  )?.status;

  // Graph state lives in the URL so a view is linkable and restorable. The
  // default focus is the most depended-upon symbol; loading it is a
  // read-only GET that records nothing.
  const focusId =
    searchParams.get("focus") ??
    components.most_depended_upon_symbols[0]?.symbol_id ??
    null;
  const directionParam = searchParams.get("direction");
  const direction: GraphDirection = DIRECTIONS.some(
    (d) => d.value === directionParam,
  )
    ? (directionParam as GraphDirection)
    : "both";
  const depth = Math.min(
    MAX_DEPTH,
    Math.max(1, Number(searchParams.get("depth")) || 2),
  );
  const limitParam = Number(searchParams.get("limit"));
  const limit = LIMITS.includes(limitParam) ? limitParam : 100;

  function update(changes: Record<string, string>) {
    const params = new URLSearchParams(searchParams.toString());
    for (const [key, value] of Object.entries(changes)) params.set(key, value);
    router.replace(`${pathname}?${params.toString()}`, { scroll: false });
  }

  return (
    <div className={styles.page}>
      <div className={styles.header}>
        <div>
          <h1 className={styles.title}>Architecture</h1>
          <p className={styles.lede}>
            The dependency graph around one symbol, as stored for this revision.
            Edges point from dependent to dependency.
          </p>
        </div>
      </div>

      {graphStatus === "not_run" ? (
        <EmptyState title="Graph not built for this revision">
          Run the revision analysis from the Overview to parse the code and
          build the graph.
        </EmptyState>
      ) : (
        <>
          <div className={styles.metrics}>
            {(
              [
                ["symbols", graph.node_count],
                ["connected symbols", graph.connected_node_count],
                ["internal edges", graph.internal_edge_count],
                ["external references", graph.external_edge_count],
              ] as const
            ).map(([label, value]) => (
              <div key={label} className={styles.metric}>
                <span className={styles.metricLabel}>{label}</span>
                <span className={styles.metricValue}>{formatCount(value)}</span>
              </div>
            ))}
          </div>

          <div className={styles.split}>
            <SymbolPicker
              revisionId={overview.revision.revision_id}
              onPick={(id) => update({ focus: id })}
            />
            <div className={styles.toolbar}>
              <label className={ui.label}>
                Direction
                <select
                  className={ui.input}
                  value={direction}
                  onChange={(e) => update({ direction: e.target.value })}
                >
                  {DIRECTIONS.map((d) => (
                    <option key={d.value} value={d.value}>
                      {d.label}
                    </option>
                  ))}
                </select>
              </label>
              <label className={ui.label}>
                Depth
                <select
                  className={ui.input}
                  value={depth}
                  onChange={(e) => update({ depth: e.target.value })}
                >
                  {Array.from({ length: MAX_DEPTH }, (_, i) => i + 1).map(
                    (d) => (
                      <option key={d} value={d}>
                        {d}
                      </option>
                    ),
                  )}
                </select>
              </label>
              <label className={ui.label}>
                Node limit
                <select
                  className={ui.input}
                  value={limit}
                  onChange={(e) => update({ limit: e.target.value })}
                >
                  {LIMITS.map((l) => (
                    <option key={l} value={l}>
                      {l}
                    </option>
                  ))}
                </select>
              </label>
            </div>
          </div>

          <Section title="Graph neighbourhood">
            {focusId ? (
              <GraphExplorer
                revisionId={overview.revision.revision_id}
                focusId={focusId}
                direction={direction}
                depth={depth}
                limit={limit}
              />
            ) : (
              <EmptyState title="No symbol focused">
                Find a symbol above to explore its neighbourhood.
              </EmptyState>
            )}
          </Section>

          <div className={styles.grid2}>
            <Section title="Hubs: most depended-upon">
              <ul className={styles.list}>
                {components.most_depended_upon_symbols.map((row) => (
                  <li key={row.symbol_id} className={styles.listItem}>
                    <button
                      type="button"
                      className={ui.rowButton}
                      aria-pressed={focusId === row.symbol_id}
                      onClick={() => update({ focus: row.symbol_id })}
                    >
                      <span className={ui.symbolName}>
                        {row.qualified_name}
                      </span>{" "}
                      <span className={ui.faint}>
                        {row.in_degree} dependents
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            </Section>
            <Section title="Edges by relationship">
              <Distribution
                rows={graph.edges_by_relationship}
                label="Edges by relationship"
              />
            </Section>
          </div>
        </>
      )}
    </div>
  );
}

export function ArchitectureView() {
  return (
    <WithOverview>
      {(overview) => <Architecture overview={overview} />}
    </WithOverview>
  );
}
