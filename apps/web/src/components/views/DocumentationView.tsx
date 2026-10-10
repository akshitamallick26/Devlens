"use client";

import { type FormEvent, useState } from "react";
import { useSearchParams } from "next/navigation";
import {
  generateDocumentation,
  getDocumentation,
  listDocumentation,
} from "@/lib/api/endpoints";
import type {
  DocumentationArtifact,
  DocumentationArtifactType,
  GenerateDocumentationRequest,
  RepositoryOverview,
} from "@/lib/api/types";
import { formatTimestamp, humanize, isUuid } from "@/lib/format";
import { useAction, useResource } from "@/lib/useApi";
import {
  Badge,
  EmptyState,
  ErrorState,
  KeyValue,
  Loading,
  Notice,
  Section,
  ui,
} from "@/components/ui/primitives";
import {
  useSelection,
  useWorkspace,
  WithOverview,
} from "@/components/workstation/WorkspaceContext";
import styles from "./views.module.css";

const ARTIFACT_TYPES: DocumentationArtifactType[] = [
  "repository_overview",
  "architecture_overview",
  "module_documentation",
  "symbol_documentation",
];

/** The two generation paths must never look alike. */
export function GenerationBadge({ status }: { status: string }) {
  if (status === "generated") return <Badge tone="info">LLM generated</Badge>;
  if (status === "fallback")
    return <Badge tone="accent">Deterministic fallback</Badge>;
  return <Badge tone="neutral">{status}</Badge>;
}

export function ArtifactDetail({
  artifact,
}: {
  artifact: DocumentationArtifact;
}) {
  const cited = new Set(artifact.cited_evidence_ids);
  return (
    <div className={styles.stack}>
      <div
        style={{
          display: "flex",
          gap: 8,
          alignItems: "center",
          flexWrap: "wrap",
        }}
      >
        <strong>{artifact.title}</strong>
        <GenerationBadge status={artifact.generation_status} />
        {artifact.context_truncated ? (
          <Badge tone="warn">context truncated</Badge>
        ) : null}
      </div>
      <KeyValue
        items={[
          ["type", humanize(artifact.artifact_type)],
          [
            "produced by",
            artifact.generation_status === "generated"
              ? `${artifact.provider || "provider not recorded"} / ${artifact.model_name || "model not recorded"}${artifact.model_version ? ` (${artifact.model_version})` : ""}`
              : "deterministic template over verified facts (no model)",
          ],
          ["revision", artifact.revision_id.slice(0, 8)],
          ["updated", formatTimestamp(artifact.updated_at)],
          [
            "evidence",
            artifact.evidence_ids.length === 0 ? (
              "none supplied"
            ) : (
              <span key="e">
                {artifact.evidence_ids.map((id) => (
                  <span
                    key={id}
                    className={ui.tag}
                    style={{
                      marginRight: 4,
                      opacity: cited.has(id) ? 1 : 0.55,
                    }}
                    title={cited.has(id) ? "cited" : "supplied, not cited"}
                  >
                    {id}
                  </span>
                ))}{" "}
                <span className={ui.faint}>
                  {artifact.cited_evidence_ids.length} cited
                </span>
              </span>
            ),
          ],
        ]}
      />
      {artifact.context_truncated ? (
        <Notice>
          The evidence for this artifact was truncated against the
          backend&apos;s context budget.
        </Notice>
      ) : null}
      {artifact.generation_status === "generated" ? (
        <p className={ui.faint}>
          Model-written prose. Structure and citations are validated by the
          backend; individual claims are not.
        </p>
      ) : null}
      {/* Rendered as plain text: artifact content may quote untrusted repository text. */}
      <div
        className={`${ui.pre} ${styles.markdown}`}
        style={{ maxHeight: "none" }}
      >
        {artifact.content}
      </div>
    </div>
  );
}

function SelectedArtifact({ artifactId }: { artifactId: string }) {
  const { repositoryId } = useWorkspace();
  const artifact = useResource(artifactId, (signal) =>
    getDocumentation(repositoryId, artifactId, signal),
  );
  if (artifact.status === "error")
    return <ErrorState error={artifact.error} onRetry={artifact.reload} />;
  if (artifact.status !== "success")
    return <Loading label="Loading artifact" />;
  return <ArtifactDetail artifact={artifact.data} />;
}

function Documentation({ overview }: { overview: RepositoryOverview }) {
  const { repositoryId } = useWorkspace();
  const { select } = useSelection();
  const searchParams = useSearchParams();
  const revisionId = overview.revision.revision_id;
  const list = useResource(`${repositoryId}:${revisionId}`, (signal) =>
    listDocumentation(repositoryId, revisionId, signal),
  );
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const initialSymbol = searchParams.get("symbol");
  const initialFile = searchParams.get("file");
  const [type, setType] = useState<DocumentationArtifactType>(
    initialSymbol
      ? "symbol_documentation"
      : initialFile
        ? "module_documentation"
        : "repository_overview",
  );
  const [target, setTarget] = useState(initialSymbol ?? initialFile ?? "");
  const generate = useAction((signal, request: GenerateDocumentationRequest) =>
    generateDocumentation(repositoryId, request, signal),
  );
  const needsTarget =
    type === "module_documentation" || type === "symbol_documentation";
  const targetValid = !needsTarget || isUuid(target.trim());

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!targetValid) return;
    generate.run({
      revision_id: revisionId,
      artifact_type: type,
      ...(type === "module_documentation"
        ? { source_file_id: target.trim() }
        : {}),
      ...(type === "symbol_documentation" ? { symbol_id: target.trim() } : {}),
    });
  }

  return (
    <div className={styles.page}>
      <div className={styles.header}>
        <div>
          <h1 className={styles.title}>Documentation</h1>
          <p className={styles.lede}>
            Documentation generated from verified repository facts. Without a
            model the backend writes a deterministic fallback; both are
            labelled.
          </p>
        </div>
      </div>

      <form className={styles.toolbar} onSubmit={submit}>
        <label className={ui.label}>
          Artifact type
          <select
            className={ui.input}
            value={type}
            onChange={(event) =>
              setType(event.target.value as DocumentationArtifactType)
            }
          >
            {ARTIFACT_TYPES.map((t) => (
              <option key={t} value={t}>
                {humanize(t)}
              </option>
            ))}
          </select>
        </label>
        {needsTarget ? (
          <label className={`${ui.label} ${styles.grow}`}>
            {type === "module_documentation" ? "Source file id" : "Symbol id"}
            <input
              className={ui.input}
              value={target}
              onChange={(event) => setTarget(event.target.value)}
              spellCheck={false}
            />
          </label>
        ) : null}
        <button
          type="submit"
          className={`${ui.button} ${ui.buttonPrimary}`}
          disabled={!targetValid || generate.status === "loading"}
        >
          Generate
        </button>
      </form>
      {needsTarget && !targetValid ? (
        <p className={ui.faint}>
          Choose a module or symbol from the overview rankings to fill this in;
          ids are not discoverable by name yet.
        </p>
      ) : null}

      {generate.status === "loading" ? (
        <Loading label="Generating documentation" />
      ) : null}
      {generate.status === "error" ? (
        <ErrorState error={generate.error} />
      ) : null}
      {generate.status === "success" ? (
        <Section title="Just generated">
          <ArtifactDetail artifact={generate.data} />
        </Section>
      ) : null}

      <div className={styles.split}>
        <Section
          title="Artifacts for this revision"
          flush
          actions={
            <button
              type="button"
              className={`${ui.button} ${ui.buttonGhost}`}
              onClick={list.reload}
            >
              Reload
            </button>
          }
        >
          {list.status === "loading" || list.status === "idle" ? (
            <Loading label="Loading artifacts" />
          ) : null}
          {list.status === "error" ? (
            <ErrorState error={list.error} onRetry={list.reload} />
          ) : null}
          {list.status === "success" && list.data.artifacts.length === 0 ? (
            <EmptyState title="No documentation for this revision" />
          ) : null}
          {list.status === "success" && list.data.artifacts.length > 0 ? (
            <ul className={styles.list} style={{ padding: "0 12px" }}>
              {list.data.artifacts.map((artifact) => (
                <li key={artifact.artifact_id} className={styles.listItem}>
                  <button
                    type="button"
                    className={ui.rowButton}
                    aria-pressed={selectedId === artifact.artifact_id}
                    onClick={() => {
                      setSelectedId(artifact.artifact_id);
                      select({
                        kind: "artifact",
                        title: artifact.title,
                        subtitle: humanize(artifact.artifact_type),
                        symbolId: artifact.target_symbol_id ?? undefined,
                        fileId: artifact.target_file_id ?? undefined,
                        facts: [
                          [
                            "generation",
                            artifact.generation_status === "generated"
                              ? "LLM generated"
                              : artifact.generation_status === "fallback"
                                ? "deterministic fallback"
                                : artifact.generation_status,
                          ],
                          ["updated", formatTimestamp(artifact.updated_at)],
                        ],
                      });
                    }}
                  >
                    <span>{artifact.title}</span>{" "}
                    <GenerationBadge status={artifact.generation_status} />{" "}
                    <span className={ui.faint}>
                      {humanize(artifact.artifact_type)}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          ) : null}
        </Section>
        <Section title="Artifact">
          {selectedId ? (
            <SelectedArtifact key={selectedId} artifactId={selectedId} />
          ) : (
            <EmptyState title="Select an artifact" />
          )}
        </Section>
      </div>
    </div>
  );
}

export function DocumentationView() {
  const searchParams = useSearchParams();
  const key = `${searchParams.get("symbol") ?? ""}:${searchParams.get("file") ?? ""}`;
  return (
    <WithOverview>
      {(overview) => <Documentation key={key} overview={overview} />}
    </WithOverview>
  );
}
