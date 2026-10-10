"use client";

import { useState } from "react";
import { analyzeRevision, getPipelineStatus } from "@/lib/api/endpoints";
import type { PipelineResult, StageAssessment } from "@/lib/api/types";
import { formatTimestamp } from "@/lib/format";
import { PIPELINE_STAGE_LABELS, pipelineStageStatus } from "@/lib/status";
import { useAction, useResource } from "@/lib/useApi";
import {
  ErrorState,
  Loading,
  Notice,
  Section,
  StatusBadge,
  ui,
} from "@/components/ui/primitives";
import styles from "@/components/views/views.module.css";

const ACTION_LABELS: Record<string, string> = {
  ran: "ran now",
  reused: "reused (already current)",
  skipped: "skipped",
  not_requested: "not requested",
};

function StageRows({
  stages,
  actions,
}: {
  stages: StageAssessment[];
  actions?: Record<string, string>;
}) {
  return (
    <div
      className={styles.statusGrid}
      role="table"
      aria-label="Analysis stages"
    >
      {stages.map((stage) => (
        <div key={stage.stage} role="row" style={{ display: "contents" }}>
          <span role="cell">
            {PIPELINE_STAGE_LABELS[stage.stage] ?? stage.stage}
          </span>
          <span role="cell" className={ui.faint}>
            {actions?.[stage.stage]
              ? `${ACTION_LABELS[actions[stage.stage]] ?? actions[stage.stage]}. `
              : ""}
            {stage.detail ??
              (stage.finished_at
                ? `finished ${formatTimestamp(stage.finished_at)}`
                : "")}
          </span>
          <span role="cell">
            <StatusBadge
              presentation={pipelineStageStatus(stage.status, stage.up_to_date)}
              raw={stage.status}
            />
          </span>
        </div>
      ))}
    </div>
  );
}

/**
 * Parsing, graph and indexing state of one revision, plus the action that
 * brings them up to date. The backend runs the pipeline synchronously, so
 * while the request is open the UI shows one in-progress state rather than
 * pretending to know which stage is executing. Loading this panel is a
 * read-only GET; only the button starts analysis.
 */
export function AnalysisPanel({
  repositoryId,
  revisionId,
  onAnalyzed,
  title = "Revision analysis",
}: {
  repositoryId: string;
  revisionId: string;
  onAnalyzed?: (result: PipelineResult) => void;
  title?: string;
}) {
  const status = useResource(`${repositoryId}:${revisionId}`, (signal) =>
    getPipelineStatus(repositoryId, revisionId, signal),
  );
  const [force, setForce] = useState(false);
  const analysis = useAction(async (signal, rebuild: boolean) => {
    const result = await analyzeRevision(
      repositoryId,
      revisionId,
      { force: rebuild },
      signal,
    );
    status.reload();
    onAnalyzed?.(result);
    return result;
  });

  const busy = analysis.status === "loading";
  const otherRunning =
    status.status === "success" && status.data.analysis_in_progress;
  const actions =
    analysis.status === "success"
      ? Object.fromEntries(analysis.data.stages.map((s) => [s.stage, s.action]))
      : undefined;
  const stages =
    analysis.status === "success"
      ? analysis.data.stages
      : status.status === "success"
        ? status.data.stages
        : null;
  const allCurrent = stages?.every((s) => s.up_to_date) ?? false;

  return (
    <Section
      title={title}
      actions={
        <div className={styles.toolbar}>
          <label className={ui.check}>
            <input
              type="checkbox"
              checked={force}
              onChange={(e) => setForce(e.target.checked)}
              disabled={busy}
            />
            Rebuild everything
          </label>
          <button
            type="button"
            className={`${ui.button} ${ui.buttonPrimary}`}
            disabled={busy || otherRunning}
            onClick={() => analysis.run(force)}
          >
            {busy
              ? "Analyzing"
              : allCurrent && !force
                ? "Re-check analysis"
                : "Analyze revision"}
          </button>
        </div>
      }
    >
      <div className={styles.stack}>
        {busy ? (
          <div role="status" aria-live="polite">
            <Notice tone="info">
              Analysis in progress. Parsing, graph construction and indexing run
              in this request; stage results appear when it finishes. There is
              no partial progress to report.
            </Notice>
          </div>
        ) : null}
        {otherRunning && !busy ? (
          <Notice>
            Another analysis of this revision is running. Reload to see its
            result when it finishes.
          </Notice>
        ) : null}
        {analysis.status === "error" ? (
          <ErrorState
            error={analysis.error}
            context={
              analysis.error.kind === "conflict"
                ? "Another analysis may already be running for this revision."
                : undefined
            }
          />
        ) : null}
        {analysis.status === "success" ? (
          <p>
            Result: <strong>{analysis.data.overall_status}</strong>
            {analysis.data.overall_status === "partial"
              ? " (some stages did not complete; see details)"
              : ""}
          </p>
        ) : null}

        {stages ? (
          <StageRows stages={stages} actions={actions} />
        ) : status.status === "error" ? (
          <ErrorState error={status.error} onRetry={status.reload} />
        ) : (
          <Loading label="Loading analysis status" />
        )}
      </div>
    </Section>
  );
}
