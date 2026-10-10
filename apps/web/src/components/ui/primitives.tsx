import type { ReactNode } from "react";
import type { ApiError } from "@/lib/api/client";
import type { CountByKey } from "@/lib/api/types";
import { formatCount, humanize } from "@/lib/format";
import {
  type StatusPresentation,
  type Tone,
  severityClass,
} from "@/lib/status";
import styles from "./ui.module.css";

export { styles as ui };

export function Badge({
  tone,
  children,
  title,
}: {
  tone: Tone;
  children: ReactNode;
  title?: string;
}) {
  return (
    <span className={`${styles.badge} ${styles[`tone-${tone}`]}`} title={title}>
      <span className={styles.dot} aria-hidden="true" />
      {children}
    </span>
  );
}

export function StatusBadge({
  presentation,
  raw,
}: {
  presentation: StatusPresentation;
  raw?: string;
}) {
  return (
    <Badge tone={presentation.tone} title={raw}>
      {presentation.label}
    </Badge>
  );
}

export function SeverityBadge({ severity }: { severity: string | null }) {
  return (
    <span className={`${styles.badge} ${styles[severityClass(severity)]}`}>
      <span className={styles.dot} aria-hidden="true" />
      {severity ?? "unknown severity"}
    </span>
  );
}

export function Section({
  title,
  actions,
  children,
  flush = false,
  id,
}: {
  title: string;
  actions?: ReactNode;
  children: ReactNode;
  flush?: boolean;
  id?: string;
}) {
  const headingId = id ? `${id}-title` : undefined;
  return (
    <section className={styles.section} aria-labelledby={headingId} id={id}>
      <header className={styles.sectionHeader}>
        <h2 className={styles.sectionTitle} id={headingId}>
          {title}
        </h2>
        {actions}
      </header>
      <div className={flush ? styles.sectionBodyFlush : styles.sectionBody}>
        {children}
      </div>
    </section>
  );
}

export function KeyValue({ items }: { items: [ReactNode, ReactNode][] }) {
  return (
    <dl className={styles.kv}>
      {items.map(([key, value], index) => (
        <div key={index} style={{ display: "contents" }}>
          <dt>{key}</dt>
          <dd>{value}</dd>
        </div>
      ))}
    </dl>
  );
}

/**
 * A bounded distribution list. Bars are relative to the largest count in
 * this list only; they are not a score.
 */
export function Distribution({
  rows,
  label,
  limit = 12,
}: {
  rows: CountByKey[];
  label: string;
  limit?: number;
}) {
  if (rows.length === 0) return <p className={styles.faint}>None recorded.</p>;
  const shown = rows.slice(0, limit);
  const max = Math.max(...shown.map((r) => r.count), 1);
  return (
    <>
      <ul className={styles.dist} aria-label={label}>
        {shown.map((row) => (
          <li key={row.key} className={styles.distRow}>
            <span className={styles.distKey} title={row.key}>
              {humanize(row.key)}
            </span>
            <span className={styles.distTrack} aria-hidden="true">
              <span
                className={styles.distFill}
                style={{ width: `${(row.count / max) * 100}%` }}
              />
            </span>
            <span className={styles.distCount}>{formatCount(row.count)}</span>
          </li>
        ))}
      </ul>
      {rows.length > limit ? (
        <p className={styles.faint}>{rows.length - limit} more not shown.</p>
      ) : null}
    </>
  );
}

export function Notice({
  children,
  tone = "warn",
}: {
  children: ReactNode;
  tone?: "warn" | "info";
}) {
  return (
    <p
      className={`${styles.notice} ${tone === "info" ? styles.noticeInfo : ""}`}
      role="note"
    >
      {children}
    </p>
  );
}

export function TruncationNotice({
  truncated,
  what,
}: {
  truncated: boolean;
  what: string;
}) {
  if (!truncated) return null;
  return (
    <Notice tone="info">
      Showing the first {what}; the backend reported more.
    </Notice>
  );
}

/** Visual placeholder only: carries no data and is labelled as loading. */
export function Loading({ label }: { label: string }) {
  return (
    <div className={styles.skeleton} role="status" aria-live="polite">
      <span className="visually-hidden">{label}</span>
      <span className={styles.skeletonBar} style={{ width: "40%" }} />
      <span className={styles.skeletonBar} style={{ width: "70%" }} />
      <span className={styles.skeletonBar} style={{ width: "55%" }} />
    </div>
  );
}

export function EmptyState({
  title,
  children,
  actions,
}: {
  title: string;
  children?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className={styles.state}>
      <p className={styles.stateTitle}>{title}</p>
      {children ? <div>{children}</div> : null}
      {actions ? <div className={styles.stateActions}>{actions}</div> : null}
    </div>
  );
}

const ERROR_TITLES: Record<ApiError["kind"], string> = {
  network: "Backend unavailable",
  not_found: "Not found",
  conflict: "Conflict",
  invalid_request: "Request rejected",
  upstream: "Upstream dependency failed",
  http: "Request failed",
  malformed_response: "Unexpected response from the API",
};

export function ErrorState({
  error,
  onRetry,
  context,
}: {
  error: ApiError;
  onRetry?: () => void;
  context?: string;
}) {
  return (
    <div className={`${styles.state} ${styles.stateError}`} role="alert">
      <p className={styles.stateTitle}>
        {ERROR_TITLES[error.kind]}
        {error.status ? (
          <span className={styles.faint}> (HTTP {error.status})</span>
        ) : null}
      </p>
      {context ? <p>{context}</p> : null}
      <p>{error.message}</p>
      {onRetry ? (
        <div className={styles.stateActions}>
          <button type="button" className={styles.button} onClick={onRetry}>
            Retry
          </button>
        </div>
      ) : null}
    </div>
  );
}
