"use client";

import Link from "next/link";
import { useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { browseFiles, getFileDetail, getSource } from "@/lib/api/endpoints";
import type {
  FileDetail,
  RepositoryOverview,
  SymbolEntry,
} from "@/lib/api/types";
import { formatBytes, formatCount } from "@/lib/format";
import { useResource } from "@/lib/useApi";
import {
  EmptyState,
  ErrorState,
  KeyValue,
  Loading,
  Notice,
  Section,
  ui,
} from "@/components/ui/primitives";
import {
  type Selection,
  useSelection,
  useWorkspace,
  WithOverview,
} from "@/components/workstation/WorkspaceContext";
import styles from "./views.module.css";

const FILES_PAGE = 100;
/** Lines requested per source window; the API caps any request at 1000. */
export const SOURCE_WINDOW = 400;

export function symbolSelection(symbol: SymbolEntry): Selection {
  return {
    kind: "symbol",
    title: symbol.qualified_name,
    subtitle: `${symbol.file_path}:${symbol.start_line}-${symbol.end_line}`,
    symbolId: symbol.symbol_id,
    fileId: symbol.file_id,
    line: symbol.start_line,
    facts: [
      ["kind", symbol.kind],
      ["lines", `${symbol.start_line} to ${symbol.end_line}`],
      ...(symbol.signature
        ? ([["signature", symbol.signature]] as [string, string][])
        : []),
    ],
  };
}

function DirectoryBrowser({
  revisionId,
  directory,
  selectedFile,
  onOpenDirectory,
  onOpenFile,
}: {
  revisionId: string;
  directory: string;
  selectedFile: string | null;
  onOpenDirectory: (path: string) => void;
  onOpenFile: (fileId: string) => void;
}) {
  const { repositoryId } = useWorkspace();
  const [offset, setOffset] = useState(0);
  const listing = useResource(
    `${revisionId}:${directory}:${offset}`,
    (signal) =>
      browseFiles(
        repositoryId,
        revisionId,
        { directory, limit: FILES_PAGE, offset },
        signal,
      ),
  );
  const parts = directory ? directory.split("/") : [];

  return (
    <Section title="Repository" flush>
      <nav
        aria-label="Directory path"
        style={{ padding: "6px 12px" }}
        className={ui.path}
      >
        <button
          type="button"
          className={ui.rowButton}
          style={{ display: "inline" }}
          onClick={() => onOpenDirectory("")}
        >
          root
        </button>
        {parts.map((part, i) => (
          <span key={i}>
            {" / "}
            <button
              type="button"
              className={ui.rowButton}
              style={{ display: "inline" }}
              onClick={() => onOpenDirectory(parts.slice(0, i + 1).join("/"))}
            >
              {part}
            </button>
          </span>
        ))}
      </nav>
      {listing.status === "loading" || listing.status === "idle" ? (
        <Loading label="Loading directory" />
      ) : null}
      {listing.status === "error" ? (
        <ErrorState error={listing.error} onRetry={listing.reload} />
      ) : null}
      {listing.status === "success" ? (
        <>
          <ul
            className={styles.list}
            style={{ padding: "0 12px" }}
            aria-label="Directory contents"
          >
            {listing.data.directories.map((dir) => (
              <li key={dir.path} className={styles.listItem}>
                <button
                  type="button"
                  className={ui.rowButton}
                  onClick={() => onOpenDirectory(dir.path)}
                >
                  <span className={ui.symbolName}>{dir.name}/</span>{" "}
                  <span className={ui.faint}>
                    {formatCount(dir.file_count)} files
                  </span>
                </button>
              </li>
            ))}
            {listing.data.files.map((file) => (
              <li key={file.file_id} className={styles.listItem}>
                <button
                  type="button"
                  className={ui.rowButton}
                  aria-current={
                    selectedFile === file.file_id ? "true" : undefined
                  }
                  onClick={() => onOpenFile(file.file_id)}
                >
                  <span
                    className={ui.symbolName}
                    style={
                      selectedFile === file.file_id
                        ? { color: "var(--accent-strong)" }
                        : undefined
                    }
                  >
                    {file.path.split("/").pop()}
                  </span>{" "}
                  <span className={ui.faint}>
                    {file.language ?? "unknown"}, {formatBytes(file.size_bytes)}
                  </span>
                </button>
              </li>
            ))}
          </ul>
          {listing.data.directories.length === 0 &&
          listing.data.files.length === 0 ? (
            <EmptyState title="Empty directory" />
          ) : null}
          {listing.data.directories_truncated ? (
            <Notice tone="info">Subdirectory list truncated.</Notice>
          ) : null}
          {listing.data.total_files > FILES_PAGE ? (
            <div className={styles.toolbar} style={{ padding: "6px 12px" }}>
              <span className={ui.faint}>
                Files {offset + 1} to{" "}
                {Math.min(offset + FILES_PAGE, listing.data.total_files)} of{" "}
                {listing.data.total_files}
              </span>
              <button
                type="button"
                className={ui.button}
                disabled={offset === 0}
                onClick={() => setOffset(Math.max(0, offset - FILES_PAGE))}
              >
                Previous
              </button>
              <button
                type="button"
                className={ui.button}
                disabled={offset + FILES_PAGE >= listing.data.total_files}
                onClick={() => setOffset(offset + FILES_PAGE)}
              >
                Next
              </button>
            </div>
          ) : null}
        </>
      ) : null}
    </Section>
  );
}

/**
 * Read-only source window. Lines are rendered as text (never HTML): the
 * content is untrusted repository input. At most SOURCE_WINDOW lines are
 * requested at a time.
 */
export function SourceViewer({
  revisionId,
  fileId,
  focusLine,
  highlight,
}: {
  revisionId: string;
  fileId: string;
  focusLine: number | null;
  highlight: { start: number; end: number } | null;
}) {
  const { repositoryId } = useWorkspace();
  const [start, setStart] = useState(
    focusLine ? Math.max(1, focusLine - 10) : 1,
  );
  const source = useResource(`${fileId}:${start}`, (signal) =>
    getSource(
      repositoryId,
      revisionId,
      fileId,
      { startLine: start, endLine: start + SOURCE_WINDOW - 1 },
      signal,
    ),
  );
  if (source.status === "error")
    return <ErrorState error={source.error} onRetry={source.reload} />;
  if (source.status !== "success") return <Loading label="Loading source" />;
  const data = source.data;
  return (
    <div className={styles.stack}>
      <div className={styles.toolbar}>
        <span className={ui.faint}>
          Lines {data.start_line} to {data.end_line} of {data.total_lines}
        </span>
        <button
          type="button"
          className={ui.button}
          disabled={data.start_line <= 1}
          onClick={() => setStart(Math.max(1, data.start_line - SOURCE_WINDOW))}
        >
          Previous {SOURCE_WINDOW}
        </button>
        <button
          type="button"
          className={ui.button}
          disabled={data.end_line >= data.total_lines}
          onClick={() => setStart(data.end_line + 1)}
        >
          Next {SOURCE_WINDOW}
        </button>
      </div>
      <div
        className={styles.source}
        role="region"
        aria-label={`Source of ${data.path}`}
        tabIndex={0}
      >
        <table>
          <tbody>
            {data.lines.map((text, index) => {
              const number = data.start_line + index;
              const marked =
                highlight !== null &&
                number >= highlight.start &&
                number <= highlight.end;
              return (
                <tr
                  key={number}
                  className={marked ? styles.sourceMarked : undefined}
                  data-line={number}
                >
                  <td className={styles.lineNumber} aria-hidden="true">
                    {number}
                  </td>
                  <td className={styles.lineText}>{text || " "}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function FilePane({
  revisionId,
  fileId,
  focusLine,
  onSymbol,
}: {
  revisionId: string;
  fileId: string;
  focusLine: number | null;
  onSymbol: (symbol: SymbolEntry) => void;
}) {
  const { repositoryId, href } = useWorkspace();
  const { selection } = useSelection();
  const detail = useResource(`${revisionId}:${fileId}`, (signal) =>
    getFileDetail(repositoryId, revisionId, fileId, signal),
  );
  if (detail.status === "error") {
    return (
      <ErrorState
        error={detail.error}
        onRetry={detail.reload}
        context={
          detail.error.kind === "not_found"
            ? "This file is not part of the revision being viewed."
            : undefined
        }
      />
    );
  }
  if (detail.status !== "success") return <Loading label="Loading file" />;
  const data: FileDetail = detail.data;
  const selected =
    data.symbols.find((s) => s.symbol_id === selection?.symbolId) ?? null;

  return (
    <div className={styles.stack}>
      <Section title={data.file.path}>
        <KeyValue
          items={[
            ["language", data.file.language ?? "unknown"],
            ["size", formatBytes(data.file.size_bytes)],
            [
              "lines",
              data.file.line_count === null
                ? "not recorded"
                : formatCount(data.file.line_count),
            ],
            ["parsing", data.file.parsing_status],
          ]}
        />
        {data.file.parsing_status !== "parsed" ? (
          <p className={ui.faint} style={{ marginTop: 6 }}>
            No symbols: this file was{" "}
            {data.file.parsing_status === "pending"
              ? "not parsed yet"
              : data.file.parsing_status}
            .
            {data.file.parsing_status === "pending" ? (
              <>
                {" "}
                <Link href={href("overview")}>Analyze the revision</Link>.
              </>
            ) : null}
          </p>
        ) : null}
      </Section>
      <div className={styles.split}>
        <Section
          title={`Symbols (${data.symbols.length}${data.symbols_truncated ? "+" : ""})`}
          flush
        >
          {data.symbols.length === 0 ? (
            <EmptyState title="No symbols" />
          ) : (
            <ul
              className={styles.list}
              style={{ padding: "0 12px", maxHeight: 520, overflow: "auto" }}
            >
              {data.symbols.map((symbol) => (
                <li key={symbol.symbol_id} className={styles.listItem}>
                  <button
                    type="button"
                    className={ui.rowButton}
                    aria-pressed={selection?.symbolId === symbol.symbol_id}
                    onClick={() => onSymbol(symbol)}
                  >
                    <span className={ui.tag}>{symbol.kind}</span>{" "}
                    <span className={ui.symbolName}>{symbol.name}</span>{" "}
                    <span className={ui.faint}>:{symbol.start_line}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Section>
        <Section title="Source">
          {data.source_available ? (
            <SourceViewer
              key={`${fileId}:${focusLine ?? 1}`}
              revisionId={revisionId}
              fileId={fileId}
              focusLine={focusLine}
              highlight={
                selected
                  ? { start: selected.start_line, end: selected.end_line }
                  : null
              }
            />
          ) : (
            <EmptyState title="Source not available">
              This file&apos;s content is stored externally and the API cannot
              serve it.
            </EmptyState>
          )}
        </Section>
      </div>
    </div>
  );
}

function Files({ overview }: { overview: RepositoryOverview }) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const { select } = useSelection();
  const directory = searchParams.get("dir") ?? "";
  const fileId = searchParams.get("file");
  const line = Number(searchParams.get("line")) || null;
  const revisionId = overview.revision.revision_id;

  function update(changes: Record<string, string | null>) {
    const params = new URLSearchParams(searchParams.toString());
    for (const [key, value] of Object.entries(changes)) {
      if (value === null) params.delete(key);
      else params.set(key, value);
    }
    router.replace(`${pathname}?${params.toString()}`, { scroll: false });
  }

  return (
    <div className={styles.page}>
      <div className={styles.header}>
        <div>
          <h1 className={styles.title}>Files</h1>
          <p className={styles.lede}>
            {formatCount(overview.statistics.stored_file_count)} stored files in
            this revision. Read-only.
          </p>
        </div>
      </div>
      <div className={styles.explorer}>
        <DirectoryBrowser
          key={directory}
          revisionId={revisionId}
          directory={directory}
          selectedFile={fileId}
          onOpenDirectory={(path) => update({ dir: path || null })}
          onOpenFile={(id) => update({ file: id, line: null })}
        />
        {fileId ? (
          <FilePane
            key={fileId}
            revisionId={revisionId}
            fileId={fileId}
            focusLine={line}
            onSymbol={(symbol) => {
              select(symbolSelection(symbol));
              update({ line: String(symbol.start_line) });
            }}
          />
        ) : (
          <EmptyState title="No file selected">
            Choose a file to see its symbols and source.
          </EmptyState>
        )}
      </div>
    </div>
  );
}

export function FilesView() {
  return (
    <WithOverview>{(overview) => <Files overview={overview} />}</WithOverview>
  );
}
