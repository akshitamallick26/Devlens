"use client";

import { type FormEvent, useState } from "react";
import { runAutomationAnalysis } from "@/lib/api/endpoints";
import type {
  AutomationResponse,
  DeletedSymbolImpact,
  RepositoryOverview,
  StageExecution,
} from "@/lib/api/types";
import { formatCount, humanize, isUuid } from "@/lib/format";
import { runStatus } from "@/lib/status";
import { useAction } from "@/lib/useApi";
import {
  Badge,
  EmptyState,
  ErrorState,
  KeyValue,
  Loading,
  Notice,
  Section,
  StatusBadge,
  ui,
} from "@/components/ui/primitives";
import {
  useWorkspace,
  WithOverview,
} from "@/components/workstation/WorkspaceContext";
import styles from "./views.module.css";

const LIST_LIMIT = 50;

/** The strategy is shown verbatim: "full_revision_rebuild" is not "success" or "incremental". */
function Stage({ stage }: { stage: StageExecution }) {
  return (
    <>
      <span>{humanize(stage.stage)}</span>
      <span>
        <Badge tone={stage.invalidated ? "warn" : "neutral"}>
          {stage.invalidated ? "invalidated" : "not invalidated"}
        </Badge>{" "}
        <code>{stage.execution_strategy}</code>
        {stage.detail ? (
          <span className={ui.faint}> {stage.detail}</span>
        ) : null}
      </span>
      <StatusBadge presentation={runStatus(stage.status)} raw={stage.status} />
    </>
  );
}

/** A deleted symbol's impact is either computed or explicitly unavailable; never "no impact". */
export function DeletedSymbolRow({ item }: { item: DeletedSymbolImpact }) {
  const unavailable = item.impact_status === "unavailable_deleted_symbol";
  return (
    <li className={styles.listItem}>
      <span className={ui.symbolName}>{item.qualified_name}</span>{" "}
      <span className={ui.path}>{item.file_path}</span>{" "}
      <Badge tone={unavailable ? "warn" : "ok"} title={item.impact_status}>
        {unavailable
          ? "impact unavailable (symbol deleted)"
          : item.impact_status}
      </Badge>
      <p className={ui.faint}>{item.reason}</p>
    </li>
  );
}

function NameList({
  title,
  names,
  total,
  truncated,
}: {
  title: string;
  names: string[];
  total: number;
  truncated: boolean;
}) {
  return (
    <div>
      <p className={ui.faint}>
        {title} ({formatCount(total)})
      </p>
      {names.length === 0 ? (
        <p className={ui.muted}>None.</p>
      ) : (
        <ul className={styles.list}>
          {names.slice(0, LIST_LIMIT).map((name) => (
            <li key={name} className={ui.path}>
              {name}
            </li>
          ))}
        </ul>
      )}
      {names.length > LIST_LIMIT || truncated ? (
        <p className={ui.faint}>
          List truncated; {formatCount(total)} in total.
        </p>
      ) : null}
    </div>
  );
}

function Results({ result }: { result: AutomationResponse }) {
  const {
    file_delta: files,
    symbol_delta: symbols,
    summary,
    impact_summary: impact,
  } = result;
  return (
    <div className={styles.stack}>
      <div className={styles.metrics}>
        {(
          [
            ["files added", summary.files_added],
            ["files modified", summary.files_modified],
            ["files deleted", summary.files_deleted],
            ["symbols added", summary.symbols_added],
            ["symbols modified", summary.symbols_modified],
            ["symbols deleted", summary.symbols_deleted],
            ["directly affected", summary.directly_affected_symbols],
            ["transitively affected", summary.transitively_affected_symbols],
          ] as const
        ).map(([label, value]) => (
          <div key={label} className={styles.metric}>
            <span className={styles.metricLabel}>{label}</span>
            <span className={styles.metricValue}>{formatCount(value)}</span>
          </div>
        ))}
      </div>

      <KeyValue
        items={[
          [
            "status",
            <StatusBadge
              key="s"
              presentation={runStatus(result.analysis_status)}
              raw={result.analysis_status}
            />,
          ],
          [
            "compared",
            `${result.parent_revision_id ? result.parent_revision_id.slice(0, 8) : "no parent"} to ${result.current_revision_id.slice(0, 8)}`,
          ],
          ["rename detection", files.rename_detection],
        ]}
      />
      <Notice tone="info">{result.execution_note}</Notice>

      <Section title="Stage execution">
        <div className={styles.statusGrid}>
          <Stage stage={result.parsing_execution} />
          <Stage stage={result.graph_execution} />
        </div>
      </Section>

      <div className={styles.grid2}>
        <Section title="Changed files">
          <div className={styles.stack}>
            <NameList
              title="Added"
              names={files.added.map((f) => f.path)}
              total={files.added_count}
              truncated={files.truncated}
            />
            <NameList
              title="Modified"
              names={files.modified.map((f) => f.path)}
              total={files.modified_count}
              truncated={files.truncated}
            />
            <NameList
              title="Deleted"
              names={files.deleted.map((f) => f.path)}
              total={files.deleted_count}
              truncated={files.truncated}
            />
          </div>
        </Section>
        <Section title="Changed symbols">
          <div className={styles.stack}>
            <NameList
              title="Added"
              names={symbols.added.map((s) => s.qualified_name)}
              total={symbols.added_count}
              truncated={symbols.truncated}
            />
            <NameList
              title="Modified"
              names={symbols.modified.map((s) => s.qualified_name)}
              total={symbols.modified_count}
              truncated={symbols.truncated}
            />
            <NameList
              title="Deleted"
              names={symbols.deleted.map((s) => s.qualified_name)}
              total={symbols.deleted_count}
              truncated={symbols.truncated}
            />
          </div>
        </Section>
      </div>

      <Section title="Impact of changes">
        {impact === null ? (
          <p className={ui.muted}>
            No impact summary was produced for this run.
          </p>
        ) : (
          <div className={styles.stack}>
            <p>
              {impact.direct.length} direct and {impact.transitive.length}{" "}
              transitive dependents across {impact.affected_files.length} files
              (max depth {impact.max_depth})
              {impact.truncated ? ", truncated" : ""}.
            </p>
            {impact.deleted_symbol_impacts.length ? (
              <div>
                <p className={ui.faint}>Deleted symbols</p>
                <ul className={styles.list}>
                  {impact.deleted_symbol_impacts
                    .slice(0, LIST_LIMIT)
                    .map((item) => (
                      <DeletedSymbolRow key={item.symbol_id} item={item} />
                    ))}
                </ul>
              </div>
            ) : null}
          </div>
        )}
      </Section>

      <Section title="Invalidation decisions">
        <KeyValue
          items={[
            [
              "RAG index",
              `${result.rag_invalidation.status}: ${result.rag_invalidation.chunks_invalidated} chunks invalidated, embeddings ${result.rag_invalidation.embeddings_require_regeneration ? "need" : "do not need"} regeneration (${result.rag_invalidation.execution_strategy})`,
            ],
            [
              "generated tests",
              `${result.testing_invalidation.stale_generated_test_ids.length} stale, ${result.testing_invalidation.changed_target_symbol_ids.length} changed targets`,
            ],
            [
              "security",
              `${result.security_invalidation.rerun_required ? "rerun required" : "no rerun required"}: ${result.security_invalidation.reason}`,
            ],
          ]}
        />
      </Section>

      {result.limitations.length ? (
        <Section title="Limitations reported by the backend">
          <ul className={styles.bullets}>
            {result.limitations.map((l) => (
              <li key={l} className={ui.muted}>
                {l}
              </li>
            ))}
          </ul>
        </Section>
      ) : null}
    </div>
  );
}

function Automation({ overview }: { overview: RepositoryOverview }) {
  const { repositoryId } = useWorkspace();
  const [parent, setParent] = useState("");
  const analysis = useAction(
    (signal, request: { revision_id: string; parent_revision_id?: string }) =>
      runAutomationAnalysis(repositoryId, request, signal),
  );
  const persisted = overview.automation;
  const parentValid = parent.trim() === "" || isUuid(parent.trim());
  const notRecorded = (v: number | null) =>
    v === null ? "not recorded" : formatCount(v);

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!parentValid) return;
    analysis.run({
      revision_id: overview.revision.revision_id,
      ...(parent.trim() ? { parent_revision_id: parent.trim() } : {}),
    });
  }

  return (
    <div className={styles.page}>
      <div className={styles.header}>
        <div>
          <h1 className={styles.title}>Change analysis</h1>
          <p className={styles.lede}>
            What changed since the parent revision, what that change affects,
            and which analyses it invalidates.
          </p>
        </div>
      </div>

      <Section
        title="Latest persisted run for this revision"
        actions={
          <StatusBadge presentation={runStatus(persisted.latest_run.status)} />
        }
      >
        {persisted.latest_run.status === "not_run" ? (
          <p className={ui.muted}>
            No change analysis has run for this revision.
          </p>
        ) : (
          <KeyValue
            items={[
              ["parent revision", persisted.parent_revision_id ?? "none"],
              ["changed files", notRecorded(persisted.changed_file_count)],
              ["changed symbols", notRecorded(persisted.changed_symbol_count)],
              [
                "impacted symbols",
                notRecorded(persisted.impacted_symbol_count),
              ],
              ["affected files", notRecorded(persisted.affected_file_count)],
              ["execution strategy", "not persisted by the backend"],
              ["invalidation decisions", "not persisted by the backend"],
            ]}
          />
        )}
      </Section>

      <form className={styles.toolbar} onSubmit={submit}>
        <label className={`${ui.label} ${styles.grow}`}>
          Parent revision id (optional; when empty the backend uses the ingested
          revision of this commit&apos;s git parent, if one exists)
          <input
            className={ui.input}
            value={parent}
            onChange={(event) => setParent(event.target.value)}
            spellCheck={false}
          />
        </label>
        <button
          type="submit"
          className={`${ui.button} ${ui.buttonPrimary}`}
          disabled={!parentValid || analysis.status === "loading"}
        >
          Run change analysis
        </button>
      </form>
      {!parentValid ? (
        <p style={{ color: "var(--danger)" }}>
          Parent revision must be a revision id.
        </p>
      ) : null}

      {analysis.status === "idle" ? (
        <EmptyState title="No change analysis run in this view yet">
          Running it recomputes the revision delta and invalidation plan;
          parsing and graph stages use a full revision rebuild.
        </EmptyState>
      ) : null}
      {analysis.status === "loading" ? (
        <Loading label="Running change analysis" />
      ) : null}
      {analysis.status === "error" ? (
        <ErrorState error={analysis.error} />
      ) : null}
      {analysis.status === "success" ? (
        <Results result={analysis.data} />
      ) : null}
    </div>
  );
}

export function AutomationView() {
  return (
    <WithOverview>
      {(overview) => <Automation overview={overview} />}
    </WithOverview>
  );
}
