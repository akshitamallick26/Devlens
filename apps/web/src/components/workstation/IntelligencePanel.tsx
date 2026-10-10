"use client";

import Link from "next/link";
import { KeyValue, ui } from "@/components/ui/primitives";
import { useSelection, useWorkspace } from "./WorkspaceContext";
import { SaveToSession } from "./SaveToSession";
import styles from "./workstation.module.css";

/**
 * Contextual detail for whatever the active view selected. It shows only
 * the facts the view already received from the API and offers navigation
 * to real backend capabilities for that entity. With nothing selected it
 * says so instead of showing placeholder intelligence.
 */
export function IntelligencePanel({
  open,
  onClose,
  closeRef,
}: {
  open: boolean;
  onClose: () => void;
  closeRef: React.RefObject<HTMLButtonElement | null>;
}) {
  const { selection, select } = useSelection();
  const { href } = useWorkspace();

  return (
    <aside
      id="intelligence-panel"
      className={`${styles.panel} ${open ? styles.panelOpen : ""}`}
      aria-label="Selection details"
    >
      <div className={styles.panelHeader}>
        <h2 className={styles.panelTitle}>
          {selection ? selection.kind : "Details"}
        </h2>
        <div className={styles.panelActions}>
          {selection ? (
            <button
              type="button"
              className={`${ui.button} ${ui.buttonGhost}`}
              onClick={() => select(null)}
            >
              Clear
            </button>
          ) : null}
          <button
            type="button"
            ref={closeRef}
            className={`${ui.button} ${ui.buttonGhost} ${styles.panelClose}`}
            onClick={onClose}
          >
            Close
          </button>
        </div>
      </div>

      <div className={styles.panelBody}>
        {selection ? (
          <>
            <div>
              <p className={ui.symbolName}>{selection.title}</p>
              {selection.subtitle ? (
                <p className={ui.path}>{selection.subtitle}</p>
              ) : null}
            </div>
            {selection.facts.length ? (
              <KeyValue items={selection.facts} />
            ) : null}
            {selection.body ? <p className={ui.pre}>{selection.body}</p> : null}
            {selection.symbolId ? (
              <div className={styles.panelActions}>
                <Link
                  className={ui.button}
                  href={href("impact", { symbol: selection.symbolId })}
                >
                  Analyze impact
                </Link>
                <Link
                  className={ui.button}
                  href={href("architecture", { focus: selection.symbolId })}
                >
                  Show in graph
                </Link>
                <Link
                  className={ui.button}
                  href={href("testing", { symbol: selection.symbolId })}
                >
                  Generate tests
                </Link>
                <Link
                  className={ui.button}
                  href={href("documentation", { symbol: selection.symbolId })}
                >
                  Document symbol
                </Link>
              </div>
            ) : null}
            {selection.fileId ? (
              <div className={styles.panelActions}>
                <Link
                  className={ui.button}
                  href={href("files", {
                    file: selection.fileId,
                    ...(selection.line ? { line: String(selection.line) } : {}),
                  })}
                >
                  Open in files
                </Link>
                {!selection.symbolId ? (
                  <Link
                    className={ui.button}
                    href={href("documentation", { file: selection.fileId })}
                  >
                    Document module
                  </Link>
                ) : null}
              </div>
            ) : null}
            <SaveToSession key={selection.title} selection={selection} />
          </>
        ) : (
          <p className={ui.muted}>
            Nothing selected. Select a symbol, module, finding, artifact or
            evidence item in the current view to inspect it here.
          </p>
        )}
      </div>
    </aside>
  );
}
