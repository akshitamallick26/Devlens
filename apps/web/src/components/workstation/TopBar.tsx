"use client";

import Link from "next/link";
import { shortSha } from "@/lib/format";
import { Badge, ui } from "@/components/ui/primitives";
import { useWorkspace } from "./WorkspaceContext";
import { RevisionSwitcher } from "./RevisionSwitcher";
import styles from "./workstation.module.css";

export function TopBar({
  onToggleNav,
  onTogglePanel,
  navOpen,
  panelOpen,
  panelToggleRef,
  navToggleRef,
}: {
  onToggleNav: () => void;
  onTogglePanel: () => void;
  navOpen: boolean;
  panelOpen: boolean;
  panelToggleRef: React.RefObject<HTMLButtonElement | null>;
  navToggleRef: React.RefObject<HTMLButtonElement | null>;
}) {
  const { overview, repositoryId, requestedRevisionId } = useWorkspace();
  const data = overview.status === "success" ? overview.data : null;

  return (
    <header className={styles.topbar}>
      <button
        type="button"
        ref={navToggleRef}
        className={`${ui.button} ${ui.buttonGhost} ${styles.menuButton}`}
        onClick={onToggleNav}
        aria-expanded={navOpen}
        aria-controls="workstation-sidebar"
      >
        Menu
      </button>

      <div className={styles.crumbs}>
        {data ? (
          <>
            <span title={data.repository.url}>
              {data.repository.owner}/<strong>{data.repository.name}</strong>
            </span>
            <span className={styles.crumbSep} aria-hidden="true">
              /
            </span>
            {/* branch_ref is shown only when ingestion recorded one. */}
            {data.revision.branch_ref ? (
              <span className={ui.tag}>{data.revision.branch_ref}</span>
            ) : null}
            <span className="mono" title={data.revision.commit_sha}>
              {shortSha(data.revision.commit_sha)}
            </span>
            {data.revision.is_current ? (
              <Badge tone="accent">current revision</Badge>
            ) : (
              <>
                <Badge tone="warn">historical revision</Badge>
                <Link
                  href={`/repositories/${repositoryId}/overview`}
                  className={ui.muted}
                >
                  view current
                </Link>
              </>
            )}
          </>
        ) : (
          <span className={ui.faint}>
            {overview.status === "error"
              ? "Repository unavailable"
              : "Loading repository"}
            {requestedRevisionId
              ? ` at revision ${requestedRevisionId.slice(0, 8)}`
              : ""}
          </span>
        )}
      </div>

      <div className={styles.topActions}>
        {data ? (
          <RevisionSwitcher currentRevisionId={data.revision.revision_id} />
        ) : null}
        <button
          type="button"
          className={ui.button}
          onClick={overview.reload}
          disabled={overview.status === "loading"}
          title="Reload the repository overview from the API"
        >
          {overview.status === "loading" ? "Loading" : "Reload"}
        </button>
        <button
          type="button"
          ref={panelToggleRef}
          className={`${ui.button} ${styles.panelButton}`}
          onClick={onTogglePanel}
          aria-expanded={panelOpen}
          aria-controls="intelligence-panel"
        >
          Details
        </button>
      </div>
    </header>
  );
}
