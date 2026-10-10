"use client";

import { type FormEvent, useState } from "react";
import { useSearchParams } from "next/navigation";
import { askQuestion, getPipelineStatus } from "@/lib/api/endpoints";
import type {
  PipelineStatus,
  QAEvidenceItem,
  QAResponse,
  RepositoryOverview,
} from "@/lib/api/types";
import { humanize, lineRange } from "@/lib/format";
import { type ResourceState, useAction, useResource } from "@/lib/useApi";
import { pipelineStageStatus } from "@/lib/status";
import {
  EmptyState,
  ErrorState,
  Loading,
  Notice,
  Section,
  StatusBadge,
  ui,
} from "@/components/ui/primitives";
import {
  type Selection,
  useSelection,
  useWorkspace,
  WithOverview,
} from "@/components/workstation/WorkspaceContext";
import styles from "./views.module.css";

const MAX_QUESTION_LENGTH = 2000;

function evidenceSelection(item: QAEvidenceItem): Selection {
  return {
    kind: "evidence",
    title: `${item.evidence_id}: ${item.qualified_name ?? item.file_path}`,
    subtitle: `${item.file_path}${lineRange(item.line_start, item.line_end)}`,
    symbolId: item.symbol_id ?? undefined,
    facts: [
      ["retrieved by", humanize(item.retrieval_source)],
      ["reason", item.retrieval_reason],
      ...(item.relationship_type
        ? ([["relationship", item.relationship_type]] as [string, string][])
        : []),
    ],
  };
}

function EvidenceList({
  items,
  cited,
}: {
  items: QAEvidenceItem[];
  cited: Set<string>;
}) {
  const { select } = useSelection();
  return (
    <ul className={styles.list}>
      {items.map((item) => (
        <li key={item.evidence_id} className={styles.listItem}>
          <button
            type="button"
            className={ui.rowButton}
            onClick={() => select(evidenceSelection(item))}
          >
            <span className={styles.evidenceId}>{item.evidence_id}</span>{" "}
            <span className={ui.symbolName}>
              {item.qualified_name ?? item.file_path}
            </span>{" "}
            <span className={ui.path}>
              {item.file_path}
              {lineRange(item.line_start, item.line_end)}
            </span>{" "}
            <span className={ui.tag}>{humanize(item.retrieval_source)}</span>
            {cited.has(item.evidence_id) ? (
              <span className={ui.faint}> cited</span>
            ) : null}
          </button>
        </li>
      ))}
    </ul>
  );
}

function Answer({ result }: { result: QAResponse }) {
  const cited = new Set(result.answer?.evidence ?? []);
  return (
    <div className={styles.stack}>
      <Section title={`Retrieved evidence (${result.evidence_items.length})`}>
        <p className={ui.faint} style={{ marginBottom: 6 }}>
          <span className={styles.factsLabel}>Retrieval</span> Deterministic
          lexical, vector and graph-expansion retrieval over this
          revision&apos;s indexed chunks.
        </p>
        {result.evidence_items.length === 0 ? (
          <EmptyState title="No evidence retrieved">
            Nothing in this revision&apos;s index matched. If the revision has
            not been indexed, Q&amp;A has nothing to retrieve.
          </EmptyState>
        ) : (
          <EvidenceList items={result.evidence_items} cited={cited} />
        )}
      </Section>

      {result.analysis_status === "retrieval_only" || !result.answer ? (
        <Notice>
          No answer was synthesized (no AI provider configured, or synthesis
          failed). The evidence above is what retrieval found; nothing has been
          summarized from it.
        </Notice>
      ) : (
        <section className={styles.explanation} aria-labelledby="qa-answer">
          <div>
            <span className={styles.explanationLabel} id="qa-answer">
              AI answer
            </span>{" "}
            <span className={ui.faint}>
              Grounded in the evidence above; [Q#] markers refer to it.
            </span>
          </div>
          <p className={styles.message}>{result.answer.answer}</p>
          {result.answer.related_symbols.length ? (
            <p className={ui.muted}>
              Related symbols:{" "}
              {result.answer.related_symbols.map((name) => (
                <span key={name} className={ui.tag} style={{ marginRight: 4 }}>
                  {name}
                </span>
              ))}
            </p>
          ) : null}
          <p className={ui.faint}>Limitations: {result.answer.limitations}</p>
          {result.answer.suggested_next_questions.length ? (
            <div>
              <h3>Possible follow-up questions</h3>
              <ul className={styles.bullets}>
                {result.answer.suggested_next_questions.map((q) => (
                  <li key={q}>{q}</li>
                ))}
              </ul>
            </div>
          ) : null}
        </section>
      )}
    </div>
  );
}

/**
 * Index status from the pipeline (which also knows when chunks were made
 * outdated by a re-parse). Q&A never falls back to ungrounded answers: with
 * no current index there is simply no evidence.
 */
function IndexState({
  pipeline,
}: {
  pipeline: ResourceState<PipelineStatus> & { reload: () => void };
}) {
  if (pipeline.status === "loading" || pipeline.status === "idle") {
    return <p className={ui.faint}>Checking index status.</p>;
  }
  if (pipeline.status === "error") {
    return (
      <ErrorState
        error={pipeline.error}
        onRetry={pipeline.reload}
        context="Index status unavailable."
      />
    );
  }
  const chunks = pipeline.data.stages.find((s) => s.stage === "rag_chunking");
  const embeddings = pipeline.data.stages.find(
    (s) => s.stage === "rag_embedding",
  );
  return (
    <div className={styles.toolbar}>
      <span className={ui.muted}>Index:</span>
      {chunks ? (
        <StatusBadge
          presentation={pipelineStageStatus(chunks.status, chunks.up_to_date)}
          raw={chunks.status}
        />
      ) : null}
      <span className={ui.muted}>Embeddings:</span>
      {embeddings ? (
        <StatusBadge
          presentation={pipelineStageStatus(
            embeddings.status,
            embeddings.up_to_date,
          )}
          raw={embeddings.status}
        />
      ) : null}
      {!chunks?.up_to_date ? (
        <Notice>
          Codebase indexing has not completed for this revision. Questions will
          return no evidence until it is analyzed from the Overview.
        </Notice>
      ) : !embeddings?.up_to_date ? (
        <span className={ui.faint}>
          Vector retrieval is unavailable (
          {embeddings?.detail ?? embeddings?.status}); lexical and graph
          retrieval still run.
        </span>
      ) : null}
    </div>
  );
}

function QAWorkbench({
  overview,
  initialQuestion,
}: {
  overview: RepositoryOverview;
  initialQuestion: string | null;
}) {
  const { repositoryId } = useWorkspace();
  const revisionId = overview.revision.revision_id;
  const [question, setQuestion] = useState(initialQuestion ?? "");
  const qa = useAction((signal, q: string) =>
    askQuestion(repositoryId, { revision_id: revisionId, question: q }, signal),
  );
  const pipeline = useResource(
    `${repositoryId}:${revisionId}:pipeline`,
    (signal) => getPipelineStatus(repositoryId, revisionId, signal),
  );

  // A question arriving in the URL (command bar, link, reload) only
  // pre-fills the form: asking calls the configured AI provider, so it
  // happens on an explicit submit, never because a page loaded.

  function submit(event: FormEvent) {
    event.preventDefault();
    if (question.trim()) qa.run(question.trim());
  }

  return (
    <div className={styles.page}>
      <div className={styles.header}>
        <div>
          <h1 className={styles.title}>Code Q&amp;A</h1>
          <p className={styles.lede}>
            Question, then retrieved evidence, then an answer grounded in that
            evidence.
          </p>
        </div>
      </div>

      <IndexState pipeline={pipeline} />

      <form className={styles.toolbar} onSubmit={submit}>
        <label className={`${ui.label} ${styles.grow}`}>
          Question
          <input
            className={ui.input}
            value={question}
            maxLength={MAX_QUESTION_LENGTH}
            onChange={(event) => setQuestion(event.target.value)}
            placeholder="Where is the session token validated?"
          />
        </label>
        <button
          type="submit"
          className={`${ui.button} ${ui.buttonPrimary}`}
          disabled={!question.trim() || qa.status === "loading"}
        >
          Ask
        </button>
      </form>

      {qa.status === "idle" ? (
        <EmptyState
          title={
            initialQuestion
              ? "Ready to ask"
              : "Ask a question about this revision"
          }
        >
          {initialQuestion
            ? "Press Ask to retrieve evidence and answer."
            : null}
        </EmptyState>
      ) : null}
      {qa.status === "loading" ? (
        <Loading label="Retrieving evidence and answering" />
      ) : null}
      {qa.status === "error" ? (
        <ErrorState error={qa.error} onRetry={() => qa.run(question.trim())} />
      ) : null}
      {qa.status === "success" ? (
        <>
          <p className={ui.muted}>
            Q: <span style={{ color: "var(--text)" }}>{qa.data.question}</span>
          </p>
          <Answer result={qa.data} />
        </>
      ) : null}
    </div>
  );
}

export function QAView() {
  const searchParams = useSearchParams();
  const initialQuestion = searchParams.get("q");
  return (
    <WithOverview>
      {(overview) => (
        <QAWorkbench
          key={initialQuestion ?? ""}
          overview={overview}
          initialQuestion={initialQuestion}
        />
      )}
    </WithOverview>
  );
}
