"use client";

import { useState } from "react";
import { runSecurityAnalysis } from "@/lib/api/endpoints";
import type {
  FindingWithContext,
  RepositoryOverview,
  ScannerResult,
  SecurityAnalysisRequest,
  SecurityAnalysisResponse,
} from "@/lib/api/types";
import { formatCount, formatTimestamp, lineRange } from "@/lib/format";
import { runStatus, scannerStatus } from "@/lib/status";
import { useAction } from "@/lib/useApi";
import {
  Distribution,
  EmptyState,
  ErrorState,
  KeyValue,
  Loading,
  Notice,
  Section,
  SeverityBadge,
  StatusBadge,
  ui,
} from "@/components/ui/primitives";
import {
  useSelection,
  useWorkspace,
  WithOverview,
} from "@/components/workstation/WorkspaceContext";
import { PersistedFindings } from "./PersistedLists";
import styles from "./views.module.css";

const FINDINGS_PAGE = 50;

/** "Scanned, nothing found" and "scanner could not run" must never look alike. */
export function ScannerOutcome({ result }: { result: ScannerResult }) {
  const presentation = scannerStatus(result.status);
  let meaning: string;
  if (result.status === "completed") {
    meaning =
      result.finding_count === 0
        ? "Scan completed with no findings."
        : `Scan completed: ${result.finding_count} findings, ${result.observation_count} observations.`;
  } else if (result.status === "unavailable") {
    meaning =
      "Scanner unavailable: this is not a clean result. Nothing was scanned by this tool.";
  } else {
    meaning = "Scanner did not complete: its results are missing, not clean.";
  }
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
        <strong>{result.scanner}</strong>
        <StatusBadge presentation={presentation} raw={result.status} />
        {result.scanner_version ? (
          <span className={ui.faint}>{result.scanner_version}</span>
        ) : null}
      </div>
      <p className={result.status === "completed" ? ui.muted : undefined}>
        {meaning}
      </p>
      {result.detail ? <p className={ui.faint}>{result.detail}</p> : null}
    </li>
  );
}

function FindingDetail({ item }: { item: FindingWithContext }) {
  const { finding, observations, blast_radius: blast, explanation } = item;
  return (
    <div className={styles.stack} style={{ padding: "8px 0" }}>
      <p className={styles.message}>{finding.message}</p>
      <KeyValue
        items={[
          ["rule", <code key="r">{finding.rule_id}</code>],
          ["analyzer", finding.analyzer],
          ["triage status", `${finding.status} (repository-wide)`],
          [
            "CVSS",
            finding.cvss_score === null
              ? "not provided"
              : `${finding.cvss_score} ${finding.cvss_vector ?? ""}`,
          ],
          ["scanner confidence", finding.scanner_confidence ?? "not provided"],
        ]}
      />
      <div>
        <p className={ui.faint}>
          Observations in this revision ({observations.length})
        </p>
        <ul className={styles.list}>
          {observations.slice(0, 20).map((obs) => (
            <li key={obs.observation_id} className={ui.path}>
              {obs.file_path
                ? `${obs.file_path}${lineRange(obs.start_line, obs.end_line)}`
                : "no file location"}
              {obs.affected_package
                ? ` | ${obs.affected_package} ${obs.installed_version ?? ""}${obs.fixed_version ? `, fixed in ${obs.fixed_version}` : ""}`
                : ""}
              {` | resolved to: ${obs.resolution_status}`}
            </li>
          ))}
        </ul>
        {observations.length > 20 ? (
          <p className={ui.faint}>
            {observations.length - 20} more observations.
          </p>
        ) : null}
      </div>
      <div>
        <p className={ui.faint}>
          Blast radius (graph reach from the affected symbol; independent of
          severity)
        </p>
        {blast ? (
          <p>
            {blast.direct_dependents.length} direct and{" "}
            {blast.transitive_dependents.length} transitive dependents across{" "}
            {blast.impacted_files.length} files, max depth{" "}
            {blast.max_depth_reached}
            {blast.truncated ? " (truncated by request limits)" : ""}.
          </p>
        ) : (
          <p className={ui.muted}>
            Not computed: the observation did not resolve to an exact symbol.
          </p>
        )}
      </div>
      {explanation ? (
        <section className={styles.explanation}>
          <div>
            <span className={styles.explanationLabel}>AI explanation</span>{" "}
            <span className={ui.faint}>
              Interprets the scanner finding and blast radius; it did not detect
              anything.
            </span>
          </div>
          <p>{explanation.summary}</p>
          <p>{explanation.why_it_matters}</p>
          <p>{explanation.blast_radius_interpretation}</p>
          <p>Remediation direction: {explanation.remediation_direction}</p>
          <p className={ui.faint}>Limitations: {explanation.limitations}</p>
        </section>
      ) : null}
    </div>
  );
}

function Results({ result }: { result: SecurityAnalysisResponse }) {
  const { select } = useSelection();
  const [expanded, setExpanded] = useState<string | null>(null);
  const [limit, setLimit] = useState(FINDINGS_PAGE);
  const scannersIncomplete = result.scanner_results.some(
    (s) => s.status !== "completed",
  );

  return (
    <div className={styles.stack}>
      <Section title="Scanners">
        <ul className={styles.list}>
          {result.scanner_results.map((scanner) => (
            <ScannerOutcome key={scanner.scanner} result={scanner} />
          ))}
        </ul>
      </Section>
      {scannersIncomplete ? (
        <Notice>
          At least one scanner did not complete. Zero findings from this run
          does not mean this revision is clean.
        </Notice>
      ) : null}

      <Section title={`Findings (${result.findings.length})`} flush>
        {result.findings.length === 0 ? (
          <EmptyState
            title={
              scannersIncomplete
                ? "No findings from the scanners that ran"
                : "No findings"
            }
          />
        ) : (
          <ul className={styles.list} style={{ padding: "0 12px" }}>
            {result.findings.slice(0, limit).map((item) => {
              const open = expanded === item.finding.finding_id;
              return (
                <li key={item.finding.finding_id} className={styles.listItem}>
                  <button
                    type="button"
                    className={ui.rowButton}
                    aria-expanded={open}
                    onClick={() => {
                      setExpanded(open ? null : item.finding.finding_id);
                      select({
                        kind: "finding",
                        title: item.finding.title,
                        subtitle: item.finding.rule_id,
                        symbolId:
                          item.observations.find((o) => o.symbol_id)
                            ?.symbol_id ?? undefined,
                        facts: [
                          ["severity", item.finding.severity ?? "unknown"],
                          ["analyzer", item.finding.analyzer],
                          ["observations", String(item.observations.length)],
                          [
                            "blast radius",
                            item.blast_radius
                              ? `${item.blast_radius.direct_dependents.length} direct, ${item.blast_radius.transitive_dependents.length} transitive`
                              : "not computed",
                          ],
                        ],
                        body: item.finding.message,
                      });
                    }}
                  >
                    <SeverityBadge severity={item.finding.severity} />{" "}
                    <strong>{item.finding.title}</strong>{" "}
                    <span className={ui.faint}>{item.finding.analyzer}</span>
                  </button>
                  {open ? <FindingDetail item={item} /> : null}
                </li>
              );
            })}
          </ul>
        )}
        {result.findings.length > limit ? (
          <div style={{ padding: "6px 12px" }}>
            <button
              type="button"
              className={ui.button}
              onClick={() => setLimit((l) => l + FINDINGS_PAGE)}
            >
              Show more ({result.findings.length - limit} remaining)
            </button>
          </div>
        ) : null}
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

function Security({ overview }: { overview: RepositoryOverview }) {
  const { repositoryId } = useWorkspace();
  const [explain, setExplain] = useState(false);
  const scan = useAction((signal, request: SecurityAnalysisRequest) =>
    runSecurityAnalysis(repositoryId, request, signal),
  );
  const persisted = overview.security;

  return (
    <div className={styles.page}>
      <div className={styles.header}>
        <div>
          <h1 className={styles.title}>Security</h1>
          <p className={styles.lede}>
            Scanner findings with their graph blast radius. Severity comes from
            the scanner; blast radius is structural reach. They are reported
            separately and never combined into a score.
          </p>
        </div>
      </div>

      <Section
        title="Persisted state for this revision"
        actions={
          <StatusBadge presentation={runStatus(persisted.latest_run.status)} />
        }
      >
        {persisted.latest_run.status === "not_run" ? (
          <p className={ui.muted}>
            No security scan has run for this revision.
          </p>
        ) : persisted.findings_run_id === null ? (
          <p className={ui.muted}>
            The latest scan did not complete ({persisted.latest_run.status}) and
            no earlier scan completed, so no finding counts exist.
          </p>
        ) : (
          <div className={styles.grid2}>
            <KeyValue
              items={[
                ["findings", formatCount(persisted.finding_count)],
                ["from run", persisted.findings_run_id.slice(0, 8)],
                [
                  "latest run finished",
                  formatTimestamp(persisted.latest_run.finished_at),
                ],
              ]}
            />
            <Distribution
              rows={persisted.findings_by_severity}
              label="Findings by severity"
            />
          </div>
        )}
        <p className={ui.faint} style={{ marginTop: 6 }}>
          Persisted findings below are from the latest completed scan. Running a
          new analysis also returns observations, blast radius and explanations.
        </p>
      </Section>

      <Section title="Findings (latest completed scan)">
        <PersistedFindings revisionId={overview.revision.revision_id} />
      </Section>

      <div className={styles.toolbar}>
        <label className={ui.check}>
          <input
            type="checkbox"
            checked={explain}
            onChange={(event) => setExplain(event.target.checked)}
          />
          Include AI explanations
        </label>
        <button
          type="button"
          className={`${ui.button} ${ui.buttonPrimary}`}
          disabled={scan.status === "loading"}
          onClick={() =>
            scan.run({
              revision_id: overview.revision.revision_id,
              include_explanation: explain,
            })
          }
        >
          Run security analysis
        </button>
        <span className={ui.faint}>
          Runs the configured scanners on the API host and records a new scan.
        </span>
      </div>

      {scan.status === "loading" ? <Loading label="Running scanners" /> : null}
      {scan.status === "error" ? <ErrorState error={scan.error} /> : null}
      {scan.status === "success" ? <Results result={scan.data} /> : null}
    </div>
  );
}

export function SecurityView() {
  return (
    <WithOverview>
      {(overview) => <Security overview={overview} />}
    </WithOverview>
  );
}
