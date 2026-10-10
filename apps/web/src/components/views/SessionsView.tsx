"use client";

import Link from "next/link";
import { useState } from "react";
import { getSession, listSessions } from "@/lib/api/endpoints";
import type {
  ContextRef,
  SessionDetail,
  SessionMessage,
  SessionSummary,
} from "@/lib/api/types";
import { formatTimestamp, humanize } from "@/lib/format";
import { useResource } from "@/lib/useApi";
import {
  Badge,
  EmptyState,
  ErrorState,
  KeyValue,
  Loading,
  Section,
  ui,
} from "@/components/ui/primitives";
import { useWorkspace } from "@/components/workstation/WorkspaceContext";
import styles from "./views.module.css";

const PAGE_SIZE = 20;

export function SessionList({ sessions }: { sessions: SessionSummary[] }) {
  const { href } = useWorkspace();
  return (
    <table className={ui.table}>
      <thead>
        <tr>
          <th scope="col">Session</th>
          <th scope="col">Status</th>
          <th scope="col" className={ui.numeric}>
            Messages
          </th>
          <th scope="col">Last active</th>
        </tr>
      </thead>
      <tbody>
        {sessions.map((session) => (
          <tr key={session.id}>
            <td>
              <Link href={href(`sessions/${session.id}`)}>
                {session.title ?? "Untitled session"}
              </Link>
            </td>
            <td>
              <Badge tone={session.status === "active" ? "accent" : "neutral"}>
                {session.status}
              </Badge>
            </td>
            <td className={ui.numeric}>{session.message_count}</td>
            <td className={ui.faint}>
              {formatTimestamp(session.last_active_at)}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** Repository-wide, ordered by last_active_at (backend order). Listing never changes recency. */
export function SessionsView() {
  const { repositoryId } = useWorkspace();
  const [offset, setOffset] = useState(0);
  const page = useResource(`${repositoryId}:${offset}`, (signal) =>
    listSessions(repositoryId, { limit: PAGE_SIZE, offset }, signal),
  );

  return (
    <div className={styles.page}>
      <div className={styles.header}>
        <div>
          <h1 className={styles.title}>Investigations</h1>
          <p className={styles.lede}>
            Investigation sessions for this repository, most recently active
            first. Opening one is read-only and does not change its recency.
          </p>
        </div>
      </div>
      <Section title="Sessions" flush>
        {page.status === "error" ? (
          <ErrorState error={page.error} onRetry={page.reload} />
        ) : null}
        {page.status === "loading" || page.status === "idle" ? (
          <Loading label="Loading sessions" />
        ) : null}
        {page.status === "success" ? (
          page.data.sessions.length === 0 ? (
            <EmptyState title="No investigation sessions">
              Sessions are created through the sessions API.
            </EmptyState>
          ) : (
            <>
              <SessionList sessions={page.data.sessions} />
              <div className={styles.toolbar} style={{ padding: "6px 12px" }}>
                <span className={ui.faint}>
                  {offset + 1} to {offset + page.data.sessions.length} of{" "}
                  {page.data.total_count}
                </span>
                <button
                  type="button"
                  className={ui.button}
                  disabled={offset === 0}
                  onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
                >
                  Previous
                </button>
                <button
                  type="button"
                  className={ui.button}
                  disabled={offset + PAGE_SIZE >= page.data.total_count}
                  onClick={() => setOffset(offset + PAGE_SIZE)}
                >
                  Next
                </button>
              </div>
            </>
          )
        ) : null}
      </Section>
    </div>
  );
}

/**
 * Restores what the backend can identify. File and symbol references are
 * ids only (there is no lookup endpoint), so they restore by linking to the
 * views that accept those ids. Analysis and finding references have no read
 * endpoint, and graph camera state belongs to a graph view this workstation
 * does not have yet; those are listed, not "restored".
 */
export function ContextRefRow({ contextRef }: { contextRef: ContextRef }) {
  const { href, repositoryId } = useWorkspace();
  let restore: React.ReactNode = (
    <span className={ui.faint}>
      Recorded; this workstation cannot restore it.
    </span>
  );
  if (contextRef.ref_type === "selected_symbol" && contextRef.symbol_id) {
    restore = (
      <Link href={href("impact", { symbol: contextRef.symbol_id })}>
        Open impact for symbol
      </Link>
    );
  } else if (contextRef.ref_type === "graph_focus") {
    // graph_focus carries only graph_camera_state; the workstation writes
    // focus_symbol_id/direction/depth/revision_id there when saving.
    const camera = contextRef.graph_camera_state ?? {};
    const focus =
      typeof camera.focus_symbol_id === "string"
        ? camera.focus_symbol_id
        : null;
    if (focus) {
      const query: Record<string, string> = { focus };
      if (typeof camera.direction === "string")
        query.direction = camera.direction;
      if (typeof camera.depth === "number") query.depth = String(camera.depth);
      const params = new URLSearchParams(query);
      if (typeof camera.revision_id === "string")
        params.set("revision", camera.revision_id);
      restore = (
        <Link
          href={`/repositories/${repositoryId}/architecture?${params.toString()}`}
        >
          Restore graph focus
        </Link>
      );
    }
  } else if (contextRef.ref_type === "selected_file" && contextRef.file_id) {
    restore = (
      <Link href={href("files", { file: contextRef.file_id })}>Open file</Link>
    );
  }
  const id =
    contextRef.symbol_id ??
    contextRef.file_id ??
    contextRef.analysis_run_id ??
    contextRef.finding_id;
  return (
    <li className={styles.listItem}>
      <span className={ui.tag}>{humanize(contextRef.ref_type)}</span>{" "}
      {id ? <code title={id}>{id.slice(0, 8)}</code> : null}{" "}
      {contextRef.graph_camera_state ? (
        <span className={ui.faint}>(graph camera state recorded) </span>
      ) : null}
      {restore}
    </li>
  );
}

function Message({ message }: { message: SessionMessage }) {
  return (
    <li className={styles.listItem}>
      <p className={ui.faint}>
        #{message.sequence_number} {message.role} |{" "}
        {formatTimestamp(message.created_at)}
      </p>
      {/* Plain text: message content is never rendered as HTML. */}
      <p className={styles.message}>{message.content}</p>
    </li>
  );
}

export function SessionDetailContent({
  session,
  onLoadOlder,
  onLatest,
}: {
  session: SessionDetail;
  onLoadOlder?: () => void;
  /** Present when an older page is shown. */
  onLatest?: () => void;
}) {
  const { href, overview } = useWorkspace();
  const viewingRevision =
    overview.status === "success" ? overview.data.revision.revision_id : null;
  const messages = [...session.messages.messages].sort(
    (a, b) => a.sequence_number - b.sequence_number,
  );

  return (
    <div className={styles.page}>
      <div className={styles.header}>
        <div>
          <h1 className={styles.title}>
            {session.title ?? "Untitled session"}
          </h1>
          <p className={styles.lede}>
            Last active {formatTimestamp(session.last_active_at)}.{" "}
            <Badge tone={session.status === "active" ? "accent" : "neutral"}>
              {session.status}
            </Badge>
          </p>
        </div>
      </div>
      <Section title="Context">
        <KeyValue
          items={[
            [
              "started at revision",
              session.started_at_revision_id
                ? session.started_at_revision_id.slice(0, 8)
                : "not recorded",
            ],
            [
              "current revision",
              session.current_revision_id ? (
                <span key="r">
                  <code>{session.current_revision_id.slice(0, 8)}</code>{" "}
                  {session.current_revision_id !== viewingRevision ? (
                    <Link
                      href={`/repositories/${session.repository_id}/overview?revision=${session.current_revision_id}`}
                    >
                      switch workstation to this revision
                    </Link>
                  ) : (
                    <span className={ui.faint}>
                      (the revision you are viewing)
                    </span>
                  )}
                </span>
              ) : (
                "not recorded"
              ),
            ],
            ["created", formatTimestamp(session.created_at)],
          ]}
        />
      </Section>
      <div className={styles.split}>
        <Section
          title={`Messages (${messages.length}${session.messages.has_more ? "+" : ""})`}
        >
          <div className={styles.toolbar} style={{ marginBottom: 8 }}>
            {session.messages.has_more && onLoadOlder ? (
              <button type="button" className={ui.button} onClick={onLoadOlder}>
                Older messages
              </button>
            ) : null}
            {onLatest ? (
              <button type="button" className={ui.button} onClick={onLatest}>
                Back to latest
              </button>
            ) : null}
          </div>
          {messages.length === 0 ? (
            <EmptyState title="No messages" />
          ) : (
            <ul className={styles.list}>
              {messages.map((m) => (
                <Message key={m.id} message={m} />
              ))}
            </ul>
          )}
        </Section>
        <Section title={`References (${session.context_refs.length})`}>
          {session.context_refs.length === 0 ? (
            <EmptyState title="No context references" />
          ) : (
            <ul className={styles.list}>
              {session.context_refs.slice(0, 200).map((ref) => (
                <ContextRefRow key={ref.id} contextRef={ref} />
              ))}
            </ul>
          )}
        </Section>
      </div>
      <p>
        <Link href={href("sessions")} className={ui.muted}>
          All investigations
        </Link>
      </p>
    </div>
  );
}

export function SessionDetailView({ sessionId }: { sessionId: string }) {
  const { repositoryId } = useWorkspace();
  const [before, setBefore] = useState<number | null>(null);
  const session = useResource(
    `${repositoryId}:${sessionId}:${before ?? "latest"}`,
    (signal) =>
      getSession(repositoryId, sessionId, { beforeSequence: before }, signal),
  );
  if (session.status === "error") {
    return (
      <ErrorState
        error={session.error}
        onRetry={session.reload}
        context={
          session.error.kind === "not_found"
            ? "This session does not exist for this repository."
            : undefined
        }
      />
    );
  }
  if (session.status !== "success") return <Loading label="Loading session" />;
  const next = session.data.messages.next_before_sequence;
  return (
    <SessionDetailContent
      session={session.data}
      onLoadOlder={next !== null ? () => setBefore(next) : undefined}
      onLatest={before !== null ? () => setBefore(null) : undefined}
    />
  );
}
