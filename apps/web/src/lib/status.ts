/**
 * Presentation mapping for backend status vocabularies. Each mapping keeps
 * the backend's own value visible and only decides tone and label. The
 * important invariants:
 *
 * - `not_run` is neutral and labelled "Not run": never "0", never "OK".
 * - A scanner that is `unavailable` is a warning, distinct from a scanner
 *   that `completed` with no findings.
 * - Unrecognized values render as neutral with their raw text, never as
 *   success.
 */

export type Tone = "ok" | "warn" | "danger" | "info" | "neutral" | "accent";

export interface StatusPresentation {
  tone: Tone;
  label: string;
}

const RUN_STATUS: Record<string, StatusPresentation> = {
  not_run: { tone: "neutral", label: "Not run" },
  pending: { tone: "info", label: "Pending" },
  running: { tone: "info", label: "Running" },
  completed: { tone: "ok", label: "Completed" },
  partial: { tone: "warn", label: "Partial" },
  failed: { tone: "danger", label: "Failed" },
  cancelled: { tone: "neutral", label: "Cancelled" },
};

const SCANNER_STATUS: Record<string, StatusPresentation> = {
  completed: { tone: "ok", label: "Scanned" },
  unavailable: { tone: "warn", label: "Scanner unavailable" },
  timeout: { tone: "danger", label: "Timed out" },
  malformed_output: { tone: "danger", label: "Malformed output" },
  error: { tone: "danger", label: "Error" },
};

const TEST_STATUS: Record<string, StatusPresentation> = {
  passed: { tone: "ok", label: "Passed" },
  failed: { tone: "danger", label: "Failed" },
  error: { tone: "danger", label: "Error" },
  timeout: { tone: "danger", label: "Timed out" },
  ready: { tone: "info", label: "Generated, not executed" },
  generated: { tone: "info", label: "Generated" },
  validation_failed: { tone: "warn", label: "Validation failed" },
  generation_failed: { tone: "danger", label: "Generation failed" },
  execution_failed: { tone: "danger", label: "Execution failed" },
  no_provider: { tone: "warn", label: "No AI provider" },
};

function lookup(
  table: Record<string, StatusPresentation>,
  value: string,
): StatusPresentation {
  return table[value] ?? { tone: "neutral", label: value };
}

export const runStatus = (value: string) => lookup(RUN_STATUS, value);
export const scannerStatus = (value: string) => lookup(SCANNER_STATUS, value);
export const testStatus = (value: string) => lookup(TEST_STATUS, value);

export function severityClass(severity: string | null): string {
  const known = ["critical", "high", "medium", "low", "info"];
  return severity && known.includes(severity)
    ? `sev-${severity}`
    : "sev-unknown";
}

/**
 * Pipeline stage presentation. A completed stage whose output was
 * invalidated by a later upstream rebuild is "Outdated", not "Completed";
 * "unavailable" is a warning, never a pass.
 */
export function pipelineStageStatus(
  status: string,
  upToDate: boolean,
): StatusPresentation {
  if (status === "completed") {
    return upToDate
      ? { tone: "ok", label: "Completed" }
      : { tone: "warn", label: "Outdated" };
  }
  if (status === "unavailable") return { tone: "warn", label: "Unavailable" };
  return runStatus(status);
}

export const PIPELINE_STAGE_LABELS: Record<string, string> = {
  parsing: "Parsing",
  graph: "Graph",
  rag_chunking: "Indexing (chunks)",
  rag_embedding: "Indexing (embeddings)",
};
