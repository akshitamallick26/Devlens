"use client";

import {
  type ReactNode,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { useSearchParams } from "next/navigation";
import { CommandBar } from "./CommandBar";
import { IntelligencePanel } from "./IntelligencePanel";
import { Sidebar } from "./Sidebar";
import { TopBar } from "./TopBar";
import { SelectionProvider, WorkspaceProvider } from "./WorkspaceContext";
import styles from "./workstation.module.css";

const NARROW_PANEL_QUERY = "(max-width: 1180px)";

function isNarrow(): boolean {
  return (
    typeof window !== "undefined" &&
    window.matchMedia(NARROW_PANEL_QUERY).matches
  );
}

/**
 * The workstation shell: sidebar, top bar, main view, intelligence panel
 * and command bar. At narrower widths the panel (and below 760px the
 * sidebar) become drawers: opening one moves focus into it, Escape closes
 * it, and focus returns to the control that opened it.
 */
export function Workstation({
  repositoryId,
  children,
}: {
  repositoryId: string;
  children: ReactNode;
}) {
  const revisionKey = useSearchParams().get("revision") ?? "current";
  const [navOpen, setNavOpen] = useState(false);
  const [panelOpen, setPanelOpen] = useState(false);
  const panelToggleRef = useRef<HTMLButtonElement>(null);
  const navToggleRef = useRef<HTMLButtonElement>(null);
  const panelCloseRef = useRef<HTMLButtonElement>(null);

  const openPanel = useCallback(() => {
    if (!isNarrow()) return;
    setPanelOpen(true);
    requestAnimationFrame(() => panelCloseRef.current?.focus());
  }, []);

  const closePanel = useCallback(() => {
    setPanelOpen(false);
    panelToggleRef.current?.focus();
  }, []);

  const closeNav = useCallback(() => setNavOpen(false), []);

  useEffect(() => {
    if (!navOpen && !panelOpen) return;
    function onKey(event: KeyboardEvent) {
      if (event.key !== "Escape") return;
      if (panelOpen) closePanel();
      if (navOpen) {
        setNavOpen(false);
        navToggleRef.current?.focus();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [navOpen, panelOpen, closePanel]);

  return (
    <WorkspaceProvider repositoryId={repositoryId}>
      {/* Keyed by revision: a selection made in revision A (symbol, file,
          finding ids) must not survive into revision B's panel. */}
      <SelectionProvider key={revisionKey} onSelect={openPanel}>
        <a href="#workstation-main" className={styles.skipLink}>
          Skip to content
        </a>
        <div className={styles.shell}>
          <Sidebar open={navOpen} onNavigate={closeNav} />
          <TopBar
            navOpen={navOpen}
            panelOpen={panelOpen}
            onToggleNav={() => setNavOpen((open) => !open)}
            onTogglePanel={() => (panelOpen ? closePanel() : openPanel())}
            panelToggleRef={panelToggleRef}
            navToggleRef={navToggleRef}
          />
          <main id="workstation-main" className={styles.main} tabIndex={-1}>
            {children}
          </main>
          <IntelligencePanel
            open={panelOpen}
            onClose={closePanel}
            closeRef={panelCloseRef}
          />
          <CommandBar />
        </div>
        {panelOpen || navOpen ? (
          <button
            type="button"
            aria-label="Close drawer"
            className={styles.backdropVisible}
            onClick={() => {
              if (panelOpen) closePanel();
              setNavOpen(false);
            }}
          />
        ) : null}
      </SelectionProvider>
    </WorkspaceProvider>
  );
}
