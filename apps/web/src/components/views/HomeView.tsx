"use client";

import Link from "next/link";
import { type FormEvent, useState, useSyncExternalStore } from "react";
import { useRouter } from "next/navigation";
import { ingestRepository } from "@/lib/api/endpoints";
import { isUuid } from "@/lib/format";
import { useAction } from "@/lib/useApi";
import {
  Badge,
  EmptyState,
  ErrorState,
  KeyValue,
  Loading,
  Section,
  ui,
} from "@/components/ui/primitives";
import { AnalysisPanel } from "@/components/pipeline/AnalysisPanel";
import styles from "./views.module.css";

/**
 * Entry point. The API has no "list repositories" endpoint, so a
 * repository is opened by id or by ingesting its URL. Ids opened in this
 * browser are remembered locally (explicit user history, not backend data)
 * and labelled as such.
 */

const STORAGE_KEY = "devlens.recentRepositoryIds";
const MAX_REMEMBERED = 8;

function readRemembered(): string[] {
  try {
    const parsed: unknown = JSON.parse(
      window.localStorage.getItem(STORAGE_KEY) ?? "[]",
    );
    return Array.isArray(parsed)
      ? parsed
          .filter((v): v is string => typeof v === "string" && isUuid(v))
          .slice(0, MAX_REMEMBERED)
      : [];
  } catch {
    return [];
  }
}

export function rememberRepository(repositoryId: string) {
  try {
    const next = [
      repositoryId,
      ...readRemembered().filter((id) => id !== repositoryId),
    ].slice(0, MAX_REMEMBERED);
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
  } catch {
    // Storage unavailable (private mode, blocked): history is a convenience only.
  }
}

const EMPTY: string[] = [];
let cachedRaw: string | null = null;
let cachedList: string[] = EMPTY;

function snapshot(): string[] {
  let raw: string | null = null;
  try {
    raw = window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return EMPTY;
  }
  if (raw !== cachedRaw) {
    cachedRaw = raw;
    cachedList = readRemembered();
  }
  return cachedList;
}

function subscribe(onChange: () => void) {
  window.addEventListener("storage", onChange);
  return () => window.removeEventListener("storage", onChange);
}

export function HomeView() {
  const router = useRouter();
  const remembered = useSyncExternalStore(subscribe, snapshot, () => EMPTY);
  const [repositoryId, setRepositoryId] = useState("");
  const [url, setUrl] = useState("");
  const ingest = useAction((signal, value: string) =>
    ingestRepository(value, signal),
  );

  function open(id: string) {
    rememberRepository(id);
    router.push(`/repositories/${id}/overview`);
  }

  function submitOpen(event: FormEvent) {
    event.preventDefault();
    const id = repositoryId.trim().toLowerCase();
    if (isUuid(id)) open(id);
  }

  function submitIngest(event: FormEvent) {
    event.preventDefault();
    if (url.trim()) ingest.run(url.trim());
  }

  return (
    <main
      className={styles.page}
      style={{ margin: "0 auto", padding: "40px 20px", maxWidth: 760 }}
    >
      <div>
        <h1 className={styles.title}>DevLens</h1>
        <p className={styles.lede}>
          Understand your code before you change it.
        </p>
      </div>

      <Section title="Open a repository">
        <form className={styles.toolbar} onSubmit={submitOpen}>
          <label className={`${ui.label} ${styles.grow}`}>
            Repository id
            <input
              className={ui.input}
              value={repositoryId}
              onChange={(event) => setRepositoryId(event.target.value)}
              placeholder="UUID returned by ingestion"
              spellCheck={false}
            />
          </label>
          <button
            type="submit"
            className={`${ui.button} ${ui.buttonPrimary}`}
            disabled={!isUuid(repositoryId.trim())}
          >
            Open
          </button>
        </form>
        {repositoryId.trim() && !isUuid(repositoryId.trim()) ? (
          <p className={ui.faint}>Repository ids are UUIDs.</p>
        ) : null}
      </Section>

      <Section title="Ingest a repository">
        <form className={styles.toolbar} onSubmit={submitIngest}>
          <label className={`${ui.label} ${styles.grow}`}>
            Public Git repository URL
            <input
              className={ui.input}
              value={url}
              onChange={(event) => setUrl(event.target.value)}
              placeholder="https://github.com/owner/name"
              spellCheck={false}
            />
          </label>
          <button
            type="submit"
            className={ui.button}
            disabled={!url.trim() || ingest.status === "loading"}
          >
            Ingest
          </button>
        </form>
        <p className={ui.faint} style={{ marginTop: 6 }}>
          Ingestion clones the default branch and stores its files. That is all
          it does: parsing, graph construction and indexing are a separate
          analysis step, offered once ingestion succeeds.
        </p>
        {ingest.status === "loading" ? (
          <Loading label="Ingesting repository" />
        ) : null}
        {ingest.status === "error" ? <ErrorState error={ingest.error} /> : null}
        {ingest.status === "success" ? (
          <div className={styles.stack} style={{ marginTop: 8 }}>
            <KeyValue
              items={[
                [
                  "status",
                  <Badge key="s" tone="ok">
                    ingested
                  </Badge>,
                ],
                [
                  "repository",
                  <code key="r">{ingest.data.repository_id}</code>,
                ],
                ["revision", <code key="v">{ingest.data.revision_id}</code>],
                ["commit", <code key="c">{ingest.data.commit_sha}</code>],
                ["files stored", String(ingest.data.file_count)],
              ]}
            />
            <AnalysisPanel
              title="Analysis (not started by ingestion)"
              repositoryId={ingest.data.repository_id}
              revisionId={ingest.data.revision_id}
            />
            <div>
              <button
                type="button"
                className={`${ui.button} ${ui.buttonPrimary}`}
                onClick={() => open(ingest.data.repository_id)}
              >
                Open workstation
              </button>
            </div>
          </div>
        ) : null}
      </Section>

      <Section title="Opened in this browser">
        {remembered.length === 0 ? (
          <EmptyState title="No repositories opened here yet">
            This list is local to this browser. The API does not provide a
            repository listing.
          </EmptyState>
        ) : (
          <ul className={styles.list}>
            {remembered.map((id) => (
              <li key={id} className={styles.listItem}>
                <Link
                  href={`/repositories/${id}/overview`}
                  className="mono"
                  onClick={() => rememberRepository(id)}
                >
                  {id}
                </Link>
              </li>
            ))}
          </ul>
        )}
      </Section>
    </main>
  );
}
