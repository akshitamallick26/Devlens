"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { shortSha } from "@/lib/format";
import { useWorkspace } from "./WorkspaceContext";
import styles from "./workstation.module.css";

/**
 * Navigation mirrors the backend's subsystems. "Change analysis" is the
 * Automation Intelligence API (revision delta + invalidation planning).
 */
export const NAV_GROUPS: {
  label: string;
  items: { section: string; label: string }[];
}[] = [
  {
    label: "Workspace",
    items: [
      { section: "overview", label: "Overview" },
      { section: "architecture", label: "Architecture" },
      { section: "impact", label: "Impact analysis" },
      { section: "qa", label: "Code Q&A" },
    ],
  },
  { label: "Code", items: [{ section: "files", label: "Files" }] },
  {
    label: "Quality",
    items: [
      { section: "testing", label: "Testing" },
      { section: "security", label: "Security" },
    ],
  },
  {
    label: "Automation",
    items: [{ section: "automation", label: "Change analysis" }],
  },
  {
    label: "Documents",
    items: [{ section: "documentation", label: "Documentation" }],
  },
  {
    label: "Sessions",
    items: [{ section: "sessions", label: "Investigations" }],
  },
];

export function Sidebar({
  open,
  onNavigate,
}: {
  open: boolean;
  onNavigate: () => void;
}) {
  const pathname = usePathname();
  const { repositoryId, overview, href } = useWorkspace();
  const base = `/repositories/${repositoryId}/`;
  const activeSection = pathname.startsWith(base)
    ? pathname.slice(base.length).split("/")[0]
    : "";

  return (
    <aside
      className={`${styles.sidebar} ${open ? styles.sidebarOpen : ""}`}
      id="workstation-sidebar"
    >
      <Link href="/workspace" className={styles.brand}>
        <span className={styles.brandMark} aria-hidden="true" />
        DevLens
      </Link>

      <div className={styles.repoIdentity}>
        {overview.status === "success" ? (
          <>
            <span
              className={styles.repoName}
              title={`${overview.data.repository.owner}/${overview.data.repository.name}`}
            >
              {overview.data.repository.owner}/{overview.data.repository.name}
            </span>
            <span className="mono" style={{ color: "var(--text-faint)" }}>
              {shortSha(overview.data.revision.commit_sha)}
              {overview.data.revision.is_current
                ? " (current)"
                : " (historical)"}
            </span>
          </>
        ) : (
          <span className="mono" style={{ color: "var(--text-faint)" }}>
            {repositoryId.slice(0, 8)}
          </span>
        )}
      </div>

      <nav className={styles.nav} aria-label="Workstation">
        {NAV_GROUPS.map((group) => (
          <div key={group.label} className={styles.navGroup}>
            <p className={styles.navGroupLabel}>{group.label}</p>
            <ul className={styles.navList}>
              {group.items.map((item) => (
                <li key={item.section}>
                  <Link
                    href={href(item.section)}
                    className={styles.navLink}
                    aria-current={
                      activeSection === item.section ? "page" : undefined
                    }
                    onClick={onNavigate}
                  >
                    {item.label}
                  </Link>
                </li>
              ))}
            </ul>
          </div>
        ))}

        {overview.status === "success" &&
        overview.data.sessions.recent_sessions.length > 0 ? (
          <div className={styles.navGroup}>
            <p className={styles.navGroupLabel}>Recent investigations</p>
            {overview.data.sessions.recent_sessions.map((session) => (
              <Link
                key={session.session_id}
                href={href(`sessions/${session.session_id}`)}
                className={styles.recent}
                title={session.title ?? "Untitled session"}
                onClick={onNavigate}
              >
                {session.title ?? "Untitled session"}
              </Link>
            ))}
          </div>
        ) : null}
      </nav>
    </aside>
  );
}
