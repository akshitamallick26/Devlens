"use client";

import Link from "next/link";
import type {
  AnalysisStatusSummary,
  ImportantComponent,
  ModuleComponent,
  RepositoryOverview,
} from "@/lib/api/types";
import {
  formatBytes,
  formatCount,
  formatTimestamp,
  humanize,
  shortSha,
} from "@/lib/format";
import { runStatus } from "@/lib/status";
import {
  Distribution,
  EmptyState,
  KeyValue,
  Notice,
  Section,
  StatusBadge,
  TruncationNotice,
  ui,
} from "@/components/ui/primitives";
import {
  type Selection,
  useSelection,
  useWorkspace,
  WithOverview,
} from "@/components/workstation/WorkspaceContext";
import { AnalysisPanel } from "@/components/pipeline/AnalysisPanel";
import styles from "./views.module.css";

/** Analysis type -> the workstation view that owns it. */
const VIEW_FOR_ANALYSIS: Record<string, string | undefined> = {
  impact_analysis: "impact",
  rag_indexing: "qa",
  security_scan: "security",
  test_generation: "testing",
  documentation_generation: "documentation",
  automation_analysis: "automation",
};

function statusOf(
  overview: RepositoryOverview,
  analysisType: string,
): AnalysisStatusSummary | undefined {
  return overview.analysis_statuses.find(
    (s) => s.analysis_type === analysisType,
  );
}

/**
 * A count that only means something once `gate` has run. When the stage has
 * never run for this revision we say so instead of printing 0.
 */
function GatedMetric({
  label,
  value,
  gate,
  notRunLabel,
}: {
  label: string;
  value: string;
  gate: AnalysisStatusSummary["status"] | undefined;
  notRunLabel: string;
}) {
  const notRun = gate === undefined || gate === "not_run";
  return (
    <div className={styles.metric}>
      <span className={styles.metricLabel}>{label}</span>
      {notRun ? (
        <span className={styles.metricNull}>{notRunLabel}</span>
      ) : (
        <span className={styles.metricValue}>{value}</span>
      )}
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string | null }) {
  return (
    <div className={styles.metric}>
      <span className={styles.metricLabel}>{label}</span>
      {value === null ? (
        <span className={styles.metricNull}>not recorded</span>
      ) : (
        <span className={styles.metricValue}>{value}</span>
      )}
    </div>
  );
}

function symbolSelection(component: ImportantComponent): Selection {
  return {
    kind: "symbol",
    title: component.qualified_name,
    subtitle: component.file_path,
    symbolId: component.symbol_id,
    facts: [
      ["kind", component.kind],
      ["direct dependents (in)", String(component.in_degree)],
      ["direct dependencies (out)", String(component.out_degree)],
    ],
  };
}

function moduleSelection(module: ModuleComponent): Selection {
  return {
    kind: "module",
    title: module.module_name ?? module.file_path,
    subtitle: module.file_path,
    fileId: module.file_id,
    facts: [
      ["dependent files (in)", String(module.in_degree)],
      ["dependency files (out)", String(module.out_degree)],
    ],
  };
}

function RankedSymbols({
  title,
  rows,
  truncated,
  metric,
}: {
  title: string;
  rows: ImportantComponent[];
  truncated: boolean;
  metric: "in_degree" | "out_degree";
}) {
  const { select } = useSelection();
  return (
    <Section title={title} flush>
      {rows.length === 0 ? (
        <EmptyState title="No ranked symbols">
          The graph has no internal edges for this metric.
        </EmptyState>
      ) : (
        <table className={ui.table}>
          <thead>
            <tr>
              <th scope="col">Symbol</th>
              <th scope="col" className={ui.numeric}>
                {metric === "in_degree" ? "Dependents" : "Dependencies"}
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.symbol_id}>
                <td>
                  <button
                    type="button"
                    className={ui.rowButton}
                    onClick={() => select(symbolSelection(row))}
                  >
                    <span className={ui.symbolName}>{row.qualified_name}</span>
                    <span className={ui.path}> {row.file_path}</span>
                  </button>
                </td>
                <td className={ui.numeric}>{row[metric]}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <TruncationNotice truncated={truncated} what={`${rows.length} symbols`} />
    </Section>
  );
}

function RankedModules({
  title,
  rows,
  truncated,
  metric,
}: {
  title: string;
  rows: ModuleComponent[];
  truncated: boolean;
  metric: "in_degree" | "out_degree";
}) {
  const { select } = useSelection();
  return (
    <Section title={title} flush>
      {rows.length === 0 ? (
        <EmptyState title="No ranked modules">
          No cross-file edges for this metric.
        </EmptyState>
      ) : (
        <table className={ui.table}>
          <thead>
            <tr>
              <th scope="col">Module</th>
              <th scope="col" className={ui.numeric}>
                {metric === "in_degree"
                  ? "Dependent files"
                  : "Dependency files"}
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.file_id}>
                <td>
                  <button
                    type="button"
                    className={ui.rowButton}
                    onClick={() => select(moduleSelection(row))}
                  >
                    <span className={ui.symbolName}>
                      {row.module_name ?? row.file_path}
                    </span>
                    {row.module_name ? (
                      <span className={ui.path}> {row.file_path}</span>
                    ) : null}
                  </button>
                </td>
                <td className={ui.numeric}>{row[metric]}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <TruncationNotice truncated={truncated} what={`${rows.length} modules`} />
    </Section>
  );
}

function RunLine({ run }: { run: AnalysisStatusSummary }) {
  return (
    <>
      <span>{humanize(run.analysis_type)}</span>
      <span className={ui.faint}>
        {run.finished_at
          ? formatTimestamp(run.finished_at)
          : run.started_at
            ? `started ${formatTimestamp(run.started_at)}`
            : ""}
        {run.error_message ? (
          <span title={run.error_message}> (error recorded)</span>
        ) : null}
      </span>
      <StatusBadge presentation={runStatus(run.status)} raw={run.status} />
    </>
  );
}

function SubsystemSummaries({ overview }: { overview: RepositoryOverview }) {
  const { href } = useWorkspace();
  const { security, testing, documentation, rag, automation } = overview;
  const countOrNull = (value: number | null) =>
    value === null ? "not recorded" : formatCount(value);

  return (
    <div className={styles.grid3}>
      <Section
        title="Security"
        actions={
          <StatusBadge presentation={runStatus(security.latest_run.status)} />
        }
      >
        {security.latest_run.status === "not_run" ? (
          <p className={ui.muted}>
            No security scan has run for this revision. No finding counts exist.
          </p>
        ) : security.findings_run_id === null ? (
          <p className={ui.muted}>
            No completed scan for this revision, so finding counts are
            unavailable.
          </p>
        ) : (
          <div className={styles.stack}>
            <KeyValue
              items={[
                [
                  "findings (latest completed scan)",
                  formatCount(security.finding_count),
                ],
              ]}
            />
            <Distribution
              rows={security.findings_by_severity}
              label="Findings by severity"
            />
            <p className={ui.faint}>
              Triage status is repository-wide, not specific to this revision.
            </p>
          </div>
        )}
        <p style={{ marginTop: 8 }}>
          <Link href={href("security")} className={ui.muted}>
            Open security
          </Link>
        </p>
      </Section>

      <Section
        title="Testing"
        actions={
          <StatusBadge presentation={runStatus(testing.latest_run.status)} />
        }
      >
        {testing.latest_run.status === "not_run" &&
        testing.generated_test_count === 0 ? (
          <p className={ui.muted}>
            No tests have been generated for this revision.
          </p>
        ) : (
          <KeyValue
            items={[
              ["generated tests", formatCount(testing.generated_test_count)],
              ["executions", formatCount(testing.execution_count)],
              ...testing.executions_by_status.map(
                (row) =>
                  [
                    `executions ${humanize(row.key)}`,
                    formatCount(row.count),
                  ] as [string, string],
              ),
            ]}
          />
        )}
        <p className={ui.faint} style={{ marginTop: 8 }}>
          Coverage is not reported by the backend.
        </p>
      </Section>

      <Section
        title="Documentation"
        actions={
          <StatusBadge
            presentation={runStatus(documentation.latest_run.status)}
          />
        }
      >
        {documentation.artifact_count === 0 ? (
          <p className={ui.muted}>
            No documentation artifacts exist for this revision.
          </p>
        ) : (
          <KeyValue
            items={[
              ["artifacts", formatCount(documentation.artifact_count)],
              ...documentation.artifacts_by_generation_method.map(
                (row) =>
                  [humanize(row.key), formatCount(row.count)] as [
                    string,
                    string,
                  ],
              ),
              [
                "last updated",
                formatTimestamp(documentation.latest_artifact_updated_at),
              ],
            ]}
          />
        )}
      </Section>

      <Section
        title="Indexing (RAG)"
        actions={
          <StatusBadge presentation={runStatus(rag.chunking_run.status)} />
        }
      >
        {rag.chunking_run.status === "not_run" ? (
          <p className={ui.muted}>
            This revision has not been chunked for retrieval. Code Q&amp;A will
            have no evidence.
          </p>
        ) : (
          <KeyValue
            items={[
              ["chunks", formatCount(rag.chunk_count)],
              ["indexed files", formatCount(rag.indexed_file_count)],
              ["embeddings", formatCount(rag.embedding_count)],
              ["embedding run", runStatus(rag.embedding_run.status).label],
            ]}
          />
        )}
      </Section>

      <Section
        title="Change analysis"
        actions={
          <StatusBadge presentation={runStatus(automation.latest_run.status)} />
        }
      >
        {automation.latest_run.status === "not_run" ? (
          <p className={ui.muted}>
            No automation analysis has run for this revision.
          </p>
        ) : (
          <KeyValue
            items={[
              [
                "parent revision",
                automation.parent_revision_id
                  ? automation.parent_revision_id.slice(0, 8)
                  : "none",
              ],
              ["changed files", countOrNull(automation.changed_file_count)],
              ["changed symbols", countOrNull(automation.changed_symbol_count)],
              [
                "impacted symbols",
                countOrNull(automation.impacted_symbol_count),
              ],
              ["affected files", countOrNull(automation.affected_file_count)],
            ]}
          />
        )}
      </Section>

      <Section title="Recent investigations">
        {overview.sessions.recent_sessions.length === 0 ? (
          <p className={ui.muted}>
            No investigation sessions for this repository.
          </p>
        ) : (
          <ul className={styles.list}>
            {overview.sessions.recent_sessions.map((session) => (
              <li key={session.session_id} className={styles.listItem}>
                <Link href={href(`sessions/${session.session_id}`)}>
                  {session.title ?? "Untitled session"}
                </Link>
                <span className={ui.faint}>
                  {" "}
                  {formatTimestamp(session.last_active_at)}
                </span>
              </li>
            ))}
          </ul>
        )}
        <p className={ui.faint} style={{ marginTop: 6 }}>
          {formatCount(overview.sessions.session_count)} sessions
          (repository-wide)
        </p>
      </Section>
    </div>
  );
}

/** Points at stages that have not run or failed, using backend statuses only. */
function NextSteps({ overview }: { overview: RepositoryOverview }) {
  const { href } = useWorkspace();
  const attention = overview.analysis_statuses.filter(
    (s) => s.status === "failed" || s.status === "partial",
  );
  const parsing = statusOf(overview, "parsing");
  const graph = statusOf(overview, "graph_build");
  const topSymbol = overview.important_components.most_depended_upon_symbols[0];

  const items: React.ReactNode[] = [];
  if (parsing?.status === "not_run" || graph?.status === "not_run") {
    items.push(
      <>
        Parsing or graph construction has not run for this revision, so symbol,
        graph and impact data are unavailable. Run the revision analysis above.
      </>,
    );
  }
  for (const run of attention) {
    const view = VIEW_FOR_ANALYSIS[run.analysis_type];
    items.push(
      <>
        {humanize(run.analysis_type)} is <strong>{run.status}</strong>
        {view ? (
          <>
            {" "}
            (<Link href={href(view)}>open</Link>)
          </>
        ) : null}
        .
      </>,
    );
  }
  if (topSymbol) {
    items.push(
      <>
        <span className={ui.symbolName}>{topSymbol.qualified_name}</span> has
        the most direct dependents ({topSymbol.in_degree}).{" "}
        <Link href={href("impact", { symbol: topSymbol.symbol_id })}>
          Analyze its impact
        </Link>
        .
      </>,
    );
  }
  if (items.length === 0) return null;
  return (
    <Section title="Inspect next">
      <ul className={styles.bullets}>
        {items.map((item, index) => (
          <li key={index}>{item}</li>
        ))}
      </ul>
    </Section>
  );
}

export function OverviewContent({
  overview,
  onAnalyzed,
}: {
  overview: RepositoryOverview;
  /** Called after an analysis request so the overview reflects new state. */
  onAnalyzed?: () => void;
}) {
  const {
    statistics,
    structure,
    graph,
    revision,
    repository,
    important_components: components,
  } = overview;
  const parsing = statusOf(overview, "parsing")?.status;
  const graphBuild = statusOf(overview, "graph_build")?.status;

  return (
    <div className={styles.page}>
      <div className={styles.header}>
        <div>
          <h1 className={styles.title}>
            {repository.owner}/{repository.name}
          </h1>
          <p className={styles.lede}>
            Revision <code>{shortSha(revision.commit_sha)}</code>
            {revision.is_current
              ? " (current)"
              : " (historical; not the repository's current revision)"}
            . Every fact below comes from this one revision unless marked
            repository-wide.
          </p>
        </div>
        <a
          href={repository.url}
          className={ui.muted}
          rel="noreferrer noopener"
          target="_blank"
        >
          {repository.url}
        </a>
      </div>

      <div className={styles.metrics} aria-label="Repository size">
        <Metric
          label="stored files"
          value={formatCount(statistics.stored_file_count)}
        />
        <Metric
          label="source size"
          value={formatBytes(statistics.total_source_bytes)}
        />
        <GatedMetric
          label="symbols"
          value={formatCount(statistics.symbol_count)}
          gate={parsing}
          notRunLabel="not parsed"
        />
        <GatedMetric
          label="python modules"
          value={formatCount(structure.python_module_count)}
          gate={parsing}
          notRunLabel="not parsed"
        />
        <GatedMetric
          label="graph edges"
          value={formatCount(graph.edge_count)}
          gate={graphBuild}
          notRunLabel="graph not built"
        />
        <GatedMetric
          label="connected symbols"
          value={`${formatCount(graph.connected_node_count)} / ${formatCount(graph.node_count)}`}
          gate={graphBuild}
          notRunLabel="graph not built"
        />
      </div>

      <AnalysisPanel
        repositoryId={repository.repository_id}
        revisionId={revision.revision_id}
        onAnalyzed={onAnalyzed}
      />

      <NextSteps overview={overview} />

      <div className={styles.grid2}>
        <Section title="Revision">
          <KeyValue
            items={[
              ["commit", <code key="c">{revision.commit_sha}</code>],
              [
                "parent",
                revision.parent_sha ? (
                  <code key="p">{revision.parent_sha}</code>
                ) : (
                  "none recorded"
                ),
              ],
              ["branch", revision.branch_ref ?? "not recorded"],
              ["committed", formatTimestamp(revision.committed_at)],
              ["discovered", formatTimestamp(revision.discovered_at)],
              [
                "ingestion status",
                revision.latest_analysis_status ?? "not recorded",
              ],
              [
                "recorded file count",
                statistics.recorded_file_count === null
                  ? "not recorded"
                  : formatCount(statistics.recorded_file_count),
              ],
            ]}
          />
        </Section>
        <Section title="Analysis status (this revision)">
          <div className={styles.statusGrid}>
            {overview.analysis_statuses.map((run) => (
              <RunLine key={run.analysis_type} run={run} />
            ))}
          </div>
        </Section>
      </div>

      <div className={styles.grid2}>
        <Section title="Top-level structure" flush>
          {structure.top_level_directories.length === 0 ? (
            <EmptyState title="No directories">
              All {structure.root_file_count} stored files are at the repository
              root.
            </EmptyState>
          ) : (
            <table className={ui.table}>
              <thead>
                <tr>
                  <th scope="col">Directory</th>
                  <th scope="col" className={ui.numeric}>
                    Files
                  </th>
                  <th scope="col" className={ui.numeric}>
                    Size
                  </th>
                  <th scope="col" className={ui.numeric}>
                    Symbols
                  </th>
                </tr>
              </thead>
              <tbody>
                {structure.top_level_directories.map((dir) => (
                  <tr key={dir.name}>
                    <td className={ui.symbolName}>{dir.name}/</td>
                    <td className={ui.numeric}>
                      {formatCount(dir.file_count)}
                    </td>
                    <td className={ui.numeric}>
                      {formatBytes(dir.total_bytes)}
                    </td>
                    <td className={ui.numeric}>
                      {parsing === "not_run"
                        ? "n/a"
                        : formatCount(dir.symbol_count)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <div style={{ padding: "6px 12px" }} className={ui.faint}>
            {formatCount(structure.root_file_count)} files at the root,{" "}
            {formatCount(structure.top_level_directory_count)} top-level
            directories.
          </div>
          <TruncationNotice
            truncated={structure.top_level_directories_truncated}
            what={`${structure.top_level_directories.length} directories`}
          />
        </Section>

        <Section title="Composition">
          <div className={styles.stack}>
            <div>
              <p className={ui.faint}>Top-level Python packages</p>
              {parsing === "not_run" ? (
                <p className={ui.muted}>Not parsed.</p>
              ) : structure.top_level_packages.length === 0 ? (
                <p className={ui.muted}>
                  No top-level packages (no &lt;dir&gt;/__init__.py module).
                </p>
              ) : (
                <p>
                  {structure.top_level_packages.map((pkg) => (
                    <span
                      key={pkg.file_path}
                      className={ui.tag}
                      title={pkg.file_path}
                      style={{ marginRight: 4 }}
                    >
                      {pkg.qualified_name}
                    </span>
                  ))}
                </p>
              )}
              {structure.top_level_packages_truncated ? (
                <Notice tone="info">
                  Package list truncated by the backend.
                </Notice>
              ) : null}
            </div>
            <div>
              <p className={ui.faint}>
                Files by language (from file extension)
              </p>
              <Distribution
                rows={statistics.files_by_language}
                label="Files by language"
              />
            </div>
            <div>
              <p className={ui.faint}>Symbols by kind</p>
              {parsing === "not_run" ? (
                <p className={ui.muted}>Not parsed.</p>
              ) : (
                <Distribution
                  rows={statistics.symbols_by_kind}
                  label="Symbols by kind"
                />
              )}
            </div>
            <div>
              <p className={ui.faint}>Graph edges by relationship</p>
              {graphBuild === "not_run" ? (
                <p className={ui.muted}>Graph not built.</p>
              ) : (
                <Distribution
                  rows={graph.edges_by_relationship}
                  label="Edges by relationship"
                />
              )}
            </div>
          </div>
        </Section>
      </div>

      <div className={styles.grid2}>
        <RankedSymbols
          title="Most depended-upon symbols"
          rows={components.most_depended_upon_symbols}
          truncated={components.most_depended_upon_symbols_truncated}
          metric="in_degree"
        />
        <RankedSymbols
          title="Most dependency-heavy symbols"
          rows={components.most_dependency_heavy_symbols}
          truncated={components.most_dependency_heavy_symbols_truncated}
          metric="out_degree"
        />
        <RankedModules
          title="Most depended-upon modules"
          rows={components.most_depended_upon_modules}
          truncated={components.most_depended_upon_modules_truncated}
          metric="in_degree"
        />
        <RankedModules
          title="Most dependency-heavy modules"
          rows={components.most_dependency_heavy_modules}
          truncated={components.most_dependency_heavy_modules_truncated}
          metric="out_degree"
        />
      </div>
      <p className={ui.faint}>
        Rankings count distinct direct dependents (in) and dependencies (out)
        over internal graph edges. They are structural counts, not risk scores.
      </p>

      <SubsystemSummaries overview={overview} />

      {overview.limitations.length ? (
        <Section title="Known limitations of this overview">
          <ul className={styles.bullets}>
            {overview.limitations.map((limitation) => (
              <li key={limitation} className={ui.muted}>
                {limitation}
              </li>
            ))}
          </ul>
        </Section>
      ) : null}
    </div>
  );
}

export function OverviewView() {
  const { overview } = useWorkspace();
  return (
    <WithOverview>
      {(data) => (
        <OverviewContent overview={data} onAnalyzed={overview.reload} />
      )}
    </WithOverview>
  );
}
