"use client";

import { type FormEvent, useState } from "react";
import { useSearchParams } from "next/navigation";
import { generateTests } from "@/lib/api/endpoints";
import type {
  GeneratedTestOutcome,
  GenerateTestsRequest,
  GenerateTestsResponse,
  RepositoryOverview,
} from "@/lib/api/types";
import { formatCount, humanize, isUuid } from "@/lib/format";
import { runStatus, testStatus } from "@/lib/status";
import { useAction } from "@/lib/useApi";
import {
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
import { PersistedTests } from "./PersistedLists";
import styles from "./views.module.css";

const MAX_SYMBOL_IDS = 50;

function Outcome({ outcome }: { outcome: GeneratedTestOutcome }) {
  return (
    <li className={styles.listItem}>
      <div
        style={{
          display: "flex",
          gap: 8,
          alignItems: "center",
          flexWrap: "wrap",
        }}
      >
        <span className={ui.symbolName}>{outcome.target_qualified_name}</span>
        <StatusBadge
          presentation={testStatus(outcome.status)}
          raw={outcome.status}
        />
        {outcome.execution ? (
          <StatusBadge
            presentation={testStatus(outcome.execution.status)}
            raw={`execution: ${outcome.execution.status}`}
          />
        ) : null}
      </div>
      <p className={ui.faint}>{outcome.selection_reason}</p>
      {outcome.error ? (
        <p style={{ color: "var(--danger)" }}>{outcome.error}</p>
      ) : null}
      {outcome.validation_issues.length ? (
        <ul className={styles.bullets}>
          {outcome.validation_issues.map((issue, i) => (
            <li key={i}>
              <code>{issue.code}</code> {issue.detail}
            </li>
          ))}
        </ul>
      ) : null}
      {outcome.execution ? (
        <details>
          <summary className={ui.muted}>
            Execution evidence: exit {outcome.execution.exit_code ?? "none"},{" "}
            {outcome.execution.duration_ms} ms
            {outcome.execution.timed_out ? ", timed out" : ""}
          </summary>
          {outcome.execution.stdout ? (
            <pre className={ui.pre}>{outcome.execution.stdout}</pre>
          ) : null}
          {outcome.execution.stderr ? (
            <pre className={ui.pre}>{outcome.execution.stderr}</pre>
          ) : null}
        </details>
      ) : outcome.status === "ready" ? (
        <p className={ui.faint}>Generated and validated, but not executed.</p>
      ) : null}
    </li>
  );
}

function Results({ result }: { result: GenerateTestsResponse }) {
  if (result.analysis_status === "no_provider") {
    return (
      <EmptyState title="No AI provider configured">
        Test generation needs a configured model provider. No tests were
        generated.
      </EmptyState>
    );
  }
  if (result.analysis_status === "no_targets") {
    return (
      <EmptyState title="No targets selected">
        The backend found no eligible symbols for the given ids.
      </EmptyState>
    );
  }
  return (
    <div className={styles.stack}>
      <Section title={`Outcomes (${result.outcomes.length})`}>
        {result.outcomes.length === 0 ? (
          <p className={ui.muted}>No outcomes returned.</p>
        ) : (
          <ul className={styles.list}>
            {result.outcomes.map((outcome) => (
              <Outcome
                key={`${outcome.target_symbol_id}:${outcome.generated_test_id ?? ""}`}
                outcome={outcome}
              />
            ))}
          </ul>
        )}
      </Section>
      <Notice tone="info">{result.limitations}</Notice>
    </div>
  );
}

function Testing({
  overview,
  initialSymbol,
}: {
  overview: RepositoryOverview;
  initialSymbol: string | null;
}) {
  const { repositoryId } = useWorkspace();
  const [symbols, setSymbols] = useState(initialSymbol ?? "");
  const [execute, setExecute] = useState(false);
  const generate = useAction((signal, request: GenerateTestsRequest) =>
    generateTests(repositoryId, request, signal),
  );
  const persisted = overview.testing;

  const ids = symbols.split(/[\s,]+/).filter(Boolean);
  const invalid = ids.filter((id) => !isUuid(id));

  function submit(event: FormEvent) {
    event.preventDefault();
    if (ids.length === 0 || invalid.length || ids.length > MAX_SYMBOL_IDS)
      return;
    generate.run({
      revision_id: overview.revision.revision_id,
      symbol_ids: ids,
      file_ids: [],
      execute,
    });
  }

  return (
    <div className={styles.page}>
      <div className={styles.header}>
        <div>
          <h1 className={styles.title}>Testing</h1>
          <p className={styles.lede}>
            Generated regression tests for chosen symbols. A generated or
            passing test is evidence about this environment, not proof the code
            is correct.
          </p>
        </div>
      </div>

      <Section
        title="Persisted state for this revision"
        actions={
          <StatusBadge presentation={runStatus(persisted.latest_run.status)} />
        }
      >
        <KeyValue
          items={[
            ["generated tests", formatCount(persisted.generated_test_count)],
            ...persisted.generated_tests_by_status.map(
              (row) =>
                [humanize(row.key), formatCount(row.count)] as [string, string],
            ),
            ["executions", formatCount(persisted.execution_count)],
            ...persisted.executions_by_status.map(
              (row) =>
                [`executed ${humanize(row.key)}`, formatCount(row.count)] as [
                  string,
                  string,
                ],
            ),
          ]}
        />
        <p className={ui.faint} style={{ marginTop: 6 }}>
          Execution output (stdout/stderr) is returned only by a generation run
          in this view; the persisted list shows statuses.
        </p>
      </Section>

      <Section title="Generated tests for this revision" flush>
        <div style={{ padding: "0 12px" }}>
          <PersistedTests revisionId={overview.revision.revision_id} />
        </div>
      </Section>

      <form className={styles.toolbar} onSubmit={submit}>
        <label className={`${ui.label} ${styles.grow}`}>
          Target symbol ids (comma or space separated, up to {MAX_SYMBOL_IDS})
          <input
            className={ui.input}
            value={symbols}
            onChange={(event) => setSymbols(event.target.value)}
            placeholder="Pick a symbol in the overview or impact view, then choose Generate tests"
            spellCheck={false}
          />
        </label>
        <label className={ui.check}>
          <input
            type="checkbox"
            checked={execute}
            onChange={(event) => setExecute(event.target.checked)}
          />
          Execute generated tests
        </label>
        <button
          type="submit"
          className={`${ui.button} ${ui.buttonPrimary}`}
          disabled={
            ids.length === 0 ||
            invalid.length > 0 ||
            ids.length > MAX_SYMBOL_IDS ||
            generate.status === "loading"
          }
        >
          Generate
        </button>
      </form>
      {invalid.length ? (
        <p style={{ color: "var(--danger)" }}>
          Not symbol ids: {invalid.slice(0, 3).join(", ")}
        </p>
      ) : null}
      {execute ? (
        <Notice>
          Execution runs generated code in a subprocess on the API host with a
          timeout and output limits. It is not an operating-system security
          sandbox.
        </Notice>
      ) : null}

      {generate.status === "idle" ? (
        <EmptyState title="No generation run in this view yet" />
      ) : null}
      {generate.status === "loading" ? (
        <Loading label="Generating tests" />
      ) : null}
      {generate.status === "error" ? (
        <ErrorState error={generate.error} />
      ) : null}
      {generate.status === "success" ? (
        <Results result={generate.data} />
      ) : null}
    </div>
  );
}

export function TestingView() {
  const searchParams = useSearchParams();
  const initialSymbol = searchParams.get("symbol");
  return (
    <WithOverview>
      {(overview) => (
        <Testing
          key={initialSymbol ?? ""}
          overview={overview}
          initialSymbol={initialSymbol}
        />
      )}
    </WithOverview>
  );
}
