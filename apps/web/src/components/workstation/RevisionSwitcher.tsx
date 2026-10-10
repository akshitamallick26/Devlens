"use client";

import Link from "next/link";
import { useState } from "react";
import { usePathname } from "next/navigation";
import { listRevisions } from "@/lib/api/endpoints";
import { formatTimestamp, shortSha } from "@/lib/format";
import { useResource } from "@/lib/useApi";
import { ErrorState, ui } from "@/components/ui/primitives";
import { useWorkspace } from "./WorkspaceContext";
import styles from "./workstation.module.css";

/**
 * Lists this repository's ingested revisions (fetched only when opened) and
 * links to the same section at another revision. The revision is URL state,
 * so switching is a navigation, never a hidden context change.
 */
export function RevisionSwitcher({
  currentRevisionId,
}: {
  currentRevisionId: string;
}) {
  const { repositoryId } = useWorkspace();
  const pathname = usePathname();
  const [open, setOpen] = useState(false);
  const revisions = useResource(
    open ? `${repositoryId}:revisions` : null,
    (signal) => listRevisions(repositoryId, { limit: 50 }, signal),
  );
  const section = pathname.split("/")[3] ?? "overview";

  return (
    <details
      className={styles.revisionSwitcher}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary className={ui.button}>Revisions</summary>
      <div className={styles.revisionMenu}>
        {revisions.status === "loading" ? (
          <p className={ui.faint}>Loading revisions</p>
        ) : null}
        {revisions.status === "error" ? (
          <ErrorState error={revisions.error} onRetry={revisions.reload} />
        ) : null}
        {revisions.status === "success" ? (
          <ul aria-label="Revisions">
            {revisions.data.revisions.map((revision) => (
              <li key={revision.revision_id}>
                <Link
                  href={`/repositories/${repositoryId}/${section}?revision=${revision.revision_id}`}
                  aria-current={
                    revision.revision_id === currentRevisionId
                      ? "true"
                      : undefined
                  }
                  className={styles.revisionItem}
                >
                  <code>{shortSha(revision.commit_sha)}</code>
                  <span className={ui.faint}>
                    {" "}
                    {formatTimestamp(revision.committed_at)}
                  </span>
                  {revision.is_current ? (
                    <span className={ui.tag}> current</span>
                  ) : null}
                </Link>
              </li>
            ))}
            {revisions.data.total_count > revisions.data.revisions.length ? (
              <li className={ui.faint}>
                Showing the newest {revisions.data.revisions.length} of{" "}
                {revisions.data.total_count}.
              </li>
            ) : null}
          </ul>
        ) : null}
      </div>
    </details>
  );
}
