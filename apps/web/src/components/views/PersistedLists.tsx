"use client";

import { useState } from "react";
import { listPersistedFindings, listPersistedTests } from "@/lib/api/endpoints";
import { formatTimestamp } from "@/lib/format";
import { type StatusPresentation, testStatus } from "@/lib/status";
import { useResource } from "@/lib/useApi";
import {
  EmptyState,
  ErrorState,
  Loading,
  SeverityBadge,
  StatusBadge,
  ui,
} from "@/components/ui/primitives";
import {
  useSelection,
  useWorkspace,
} from "@/components/workstation/WorkspaceContext";
import styles from "./views.module.css";

const PAGE = 50;

/** Validation is static checking of the generated file, not execution. */
const VALIDATION: Record<string, StatusPresentation> = {
  passed: { tone: "ok", label: "Valid" },
  failed: { tone: "warn", label: "Invalid" },
  pending: { tone: "neutral", label: "Not validated" },
};

function Pager({
  offset,
  total,
  onChange,
}: {
  offset: number;
  total: number;
  onChange: (offset: number) => void;
}) {
  if (total <= PAGE) return null;
  return (
    <div className={styles.toolbar} style={{ padding: "6px 0" }}>
      <span className={ui.faint}>
        {offset + 1} to {Math.min(offset + PAGE, total)} of {total}
      </span>
      <button
        type="button"
        className={ui.button}
        disabled={offset === 0}
        onClick={() => onChange(Math.max(0, offset - PAGE))}
      >
        Previous
      </button>
      <button
        type="button"
        className={ui.button}
        disabled={offset + PAGE >= total}
        onClick={() => onChange(offset + PAGE)}
      >
        Next
      </button>
    </div>
  );
}

/** Findings of the latest completed scan of this revision, as persisted. */
export function PersistedFindings({ revisionId }: { revisionId: string }) {
  const { repositoryId } = useWorkspace();
  const { select } = useSelection();
  const [offset, setOffset] = useState(0);
  const page = useResource(`${revisionId}:findings:${offset}`, (signal) =>
    listPersistedFindings(
      repositoryId,
      revisionId,
      { limit: PAGE, offset },
      signal,
    ),
  );
  if (page.status === "error")
    return <ErrorState error={page.error} onRetry={page.reload} />;
  if (page.status !== "success") return <Loading label="Loading findings" />;
  if (page.data.findings_run_id === null) {
    return (
      <p className={ui.muted}>
        No completed security scan for this revision, so there are no persisted
        findings.
      </p>
    );
  }
  if (page.data.findings.length === 0) {
    return (
      <EmptyState title="The latest completed scan recorded no findings" />
    );
  }
  return (
    <>
      <ul className={styles.list} aria-label="Persisted findings">
        {page.data.findings.map((finding) => (
          <li key={finding.finding_id} className={styles.listItem}>
            <button
              type="button"
              className={ui.rowButton}
              onClick={() =>
                select({
                  kind: "finding",
                  title: finding.title,
                  subtitle: finding.rule_id,
                  findingId: finding.finding_id,
                  body: finding.message,
                  facts: [
                    ["severity", finding.severity ?? "unknown"],
                    ["analyzer", finding.analyzer],
                    [
                      "triage status",
                      `${finding.triage_status} (repository-wide)`,
                    ],
                    ["observations", String(finding.observation_count)],
                    [
                      "first location",
                      finding.first_location ?? "none recorded",
                    ],
                  ],
                })
              }
            >
              <SeverityBadge severity={finding.severity} />{" "}
              <strong>{finding.title}</strong>{" "}
              <span className={ui.faint}>
                {finding.analyzer}
                {finding.first_location ? `, ${finding.first_location}` : ""}
              </span>
            </button>
          </li>
        ))}
      </ul>
      <Pager
        offset={offset}
        total={page.data.total_count}
        onChange={setOffset}
      />
    </>
  );
}

/** Generated tests of this revision with their latest execution, as persisted. */
export function PersistedTests({ revisionId }: { revisionId: string }) {
  const { repositoryId } = useWorkspace();
  const [offset, setOffset] = useState(0);
  const page = useResource(`${revisionId}:tests:${offset}`, (signal) =>
    listPersistedTests(
      repositoryId,
      revisionId,
      { limit: PAGE, offset },
      signal,
    ),
  );
  if (page.status === "error")
    return <ErrorState error={page.error} onRetry={page.reload} />;
  if (page.status !== "success")
    return <Loading label="Loading generated tests" />;
  if (page.data.tests.length === 0)
    return (
      <p className={ui.muted}>
        No tests have been generated for this revision.
      </p>
    );
  return (
    <>
      <table className={ui.table}>
        <thead>
          <tr>
            <th scope="col">Target</th>
            <th scope="col">Validation</th>
            <th scope="col">Latest execution</th>
            <th scope="col">Generated</th>
          </tr>
        </thead>
        <tbody>
          {page.data.tests.map((test) => (
            <tr key={test.generated_test_id}>
              <td>
                <span className={ui.symbolName}>
                  {test.target_qualified_name ??
                    test.target_symbol_id.slice(0, 8)}
                </span>
                <div className={ui.path}>{test.proposed_path}</div>
              </td>
              <td>
                <StatusBadge
                  presentation={
                    VALIDATION[test.validation_status] ?? {
                      tone: "neutral",
                      label: test.validation_status,
                    }
                  }
                  raw={`validation: ${test.validation_status}`}
                />
              </td>
              <td>
                {test.latest_execution ? (
                  <StatusBadge
                    presentation={testStatus(test.latest_execution.status)}
                    raw={test.latest_execution.status}
                  />
                ) : (
                  <span className={ui.faint}>not executed</span>
                )}
              </td>
              <td className={ui.faint}>{formatTimestamp(test.created_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <Pager
        offset={offset}
        total={page.data.total_count}
        onChange={setOffset}
      />
    </>
  );
}
