"use client";

import { type FormEvent, useState } from "react";
import { useSearchParams } from "next/navigation";
import { ApiError } from "@/lib/api/client";
import { analyzeImpact } from "@/lib/api/endpoints";
import type {
  ImpactAmbiguity,
  ImpactRequest,
  RepositoryOverview,
} from "@/lib/api/types";
import { isUuid } from "@/lib/format";
import { useAction } from "@/lib/useApi";
import {
  EmptyState,
  ErrorState,
  Loading,
  Notice,
  Section,
  ui,
} from "@/components/ui/primitives";
import {
  useWorkspace,
  WithOverview,
} from "@/components/workstation/WorkspaceContext";
import { ImpactResult } from "./ImpactResult";
import styles from "./views.module.css";

function isAmbiguity(detail: unknown): detail is ImpactAmbiguity {
  return Boolean(
    detail &&
    typeof detail === "object" &&
    Array.isArray((detail as ImpactAmbiguity).candidates),
  );
}

/** Ambiguity (HTTP 409) is shown as a choice, never resolved silently. */
function AmbiguityChooser({
  ambiguity,
  onChoose,
}: {
  ambiguity: ImpactAmbiguity;
  onChoose: (symbolId: string) => void;
}) {
  return (
    <Section title="Ambiguous target">
      <p className={ui.muted}>{ambiguity.message}. Choose the exact symbol:</p>
      <ul className={styles.list}>
        {ambiguity.candidates.map((candidate) => (
          <li key={candidate.symbol_id} className={styles.listItem}>
            <button
              type="button"
              className={ui.rowButton}
              onClick={() => onChoose(candidate.symbol_id)}
            >
              <span className={ui.tag}>{candidate.symbol_kind}</span>{" "}
              <span className={ui.path}>{candidate.file_path}</span>{" "}
              <span className={ui.faint}>
                {candidate.symbol_id.slice(0, 8)}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </Section>
  );
}

function ImpactWorkbench({ overview }: { overview: RepositoryOverview }) {
  const { repositoryId } = useWorkspace();
  const searchParams = useSearchParams();
  const revisionId = overview.revision.revision_id;
  const initialSymbol = searchParams.get("symbol");
  const initialName = searchParams.get("qualified_name");

  const [target, setTarget] = useState(initialName ?? initialSymbol ?? "");
  const [explain, setExplain] = useState(false);
  const impact = useAction((signal, request: ImpactRequest) =>
    analyzeImpact(repositoryId, request, signal),
  );

  function run(value: string, includeExplanation: boolean) {
    const trimmed = value.trim();
    if (!trimmed) return;
    impact.run({
      revision_id: revisionId,
      include_explanation: includeExplanation,
      ...(isUuid(trimmed)
        ? { symbol_id: trimmed }
        : { qualified_name: trimmed }),
    });
  }

  // A target arriving in the URL (from a link, the command bar or a saved
  // investigation) only pre-fills the form. Running impact analysis records
  // an AnalysisRun, so it never happens just because a page loaded.
  const initial = initialSymbol ?? initialName;

  function submit(event: FormEvent) {
    event.preventDefault();
    run(target, explain);
  }

  const graphReady = overview.analysis_statuses.find(
    (s) => s.analysis_type === "graph_build",
  )?.status;

  return (
    <div className={styles.page}>
      <div className={styles.header}>
        <div>
          <h1 className={styles.title}>Impact analysis</h1>
          <p className={styles.lede}>
            Change a symbol: what depends on it, directly and transitively, in
            this revision.
          </p>
        </div>
      </div>

      {graphReady === "not_run" ? (
        <Notice>
          The dependency graph has not been built for this revision, so every
          symbol will report no dependents. Analyze the revision from the
          Overview first.
        </Notice>
      ) : null}

      <form className={styles.toolbar} onSubmit={submit}>
        <label className={`${ui.label} ${styles.grow}`}>
          Target (qualified name or symbol id)
          <input
            className={ui.input}
            value={target}
            onChange={(event) => setTarget(event.target.value)}
            placeholder="package.module.function"
            spellCheck={false}
          />
        </label>
        <label className={ui.check}>
          <input
            type="checkbox"
            checked={explain}
            onChange={(event) => setExplain(event.target.checked)}
          />
          Include AI explanation
        </label>
        <button
          type="submit"
          className={`${ui.button} ${ui.buttonPrimary}`}
          disabled={!target.trim() || impact.status === "loading"}
        >
          Analyze
        </button>
      </form>

      {impact.status === "idle" ? (
        initial ? (
          <EmptyState
            title="Ready to analyze"
            actions={
              <button
                type="button"
                className={`${ui.button} ${ui.buttonPrimary}`}
                onClick={() => run(initial, explain)}
              >
                Analyze this target
              </button>
            }
          >
            <code>{initial}</code> in this revision. Analysis runs when you ask.
          </EmptyState>
        ) : (
          <EmptyState title="No target analyzed yet">
            Enter a fully qualified name, or pick a symbol from the overview,
            files or architecture views.
          </EmptyState>
        )
      ) : null}
      {impact.status === "loading" ? (
        <Loading label="Running impact analysis" />
      ) : null}
      {impact.status === "error" ? (
        impact.error.kind === "conflict" && isAmbiguity(impact.error.detail) ? (
          <AmbiguityChooser
            ambiguity={impact.error.detail}
            onChoose={(symbolId) => {
              setTarget(symbolId);
              run(symbolId, explain);
            }}
          />
        ) : impact.error.kind === "not_found" ? (
          <EmptyState title="Target not found in this revision">
            {impact.error.message}. Qualified names are exact (for example{" "}
            <code>package.module.Class.method</code>).
          </EmptyState>
        ) : (
          <ErrorState
            error={impact.error as ApiError}
            onRetry={() => run(target, explain)}
          />
        )
      ) : null}
      {impact.status === "success" ? (
        <ImpactResult result={impact.data} />
      ) : null}
    </div>
  );
}

export function ImpactView() {
  const searchParams = useSearchParams();
  // A new target from a link or the command bar starts a fresh workbench.
  const key = `${searchParams.get("symbol") ?? ""}:${searchParams.get("qualified_name") ?? ""}`;
  return (
    <WithOverview>
      {(overview) => <ImpactWorkbench key={key} overview={overview} />}
    </WithOverview>
  );
}
