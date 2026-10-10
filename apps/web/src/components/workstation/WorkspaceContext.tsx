"use client";

import {
  createContext,
  type ReactNode,
  useContext,
  useMemo,
  useState,
} from "react";
import { useSearchParams } from "next/navigation";
import { getOverview } from "@/lib/api/endpoints";
import type { RepositoryOverview } from "@/lib/api/types";
import { useResource, type ResourceState } from "@/lib/useApi";
import { Loading, ErrorState } from "@/components/ui/primitives";

/**
 * Workspace state lives in the URL: the repository id is the route segment
 * and an explicit revision is the `?revision=` search param (absent means
 * the repository's current revision, resolved by the backend). The
 * overview for that revision is fetched once here and shared, so the top
 * bar, sidebar and every view agree on exactly one revision.
 */

export interface WorkspaceValue {
  repositoryId: string;
  /** The `?revision=` param, or null for "current revision". */
  requestedRevisionId: string | null;
  overview: ResourceState<RepositoryOverview> & { reload: () => void };
  /** Builds a workstation href that keeps the revision selection. */
  href: (section: string, query?: Record<string, string>) => string;
}

const WorkspaceContext = createContext<WorkspaceValue | null>(null);

export function WorkspaceProvider({
  repositoryId,
  children,
}: {
  repositoryId: string;
  children: ReactNode;
}) {
  const searchParams = useSearchParams();
  const requestedRevisionId = searchParams.get("revision");
  const overview = useResource(
    `${repositoryId}:${requestedRevisionId ?? "current"}`,
    (signal) => getOverview(repositoryId, requestedRevisionId, signal),
  );

  const value = useMemo<WorkspaceValue>(
    () => ({
      repositoryId,
      requestedRevisionId,
      overview,
      href: (section, query = {}) => {
        const params = new URLSearchParams(query);
        if (requestedRevisionId) params.set("revision", requestedRevisionId);
        const qs = params.toString();
        return `/repositories/${repositoryId}/${section}${qs ? `?${qs}` : ""}`;
      },
    }),
    [repositoryId, requestedRevisionId, overview],
  );

  return (
    <WorkspaceContext.Provider value={value}>
      {children}
    </WorkspaceContext.Provider>
  );
}

export function useWorkspace(): WorkspaceValue {
  const value = useContext(WorkspaceContext);
  if (!value)
    throw new Error("useWorkspace must be used inside WorkspaceProvider");
  return value;
}

/**
 * Renders `children` with the resolved overview, or the shared loading /
 * error state. Every view that needs the revision id goes through this, so
 * no view can act on a revision the shell is not showing.
 */
export function WithOverview({
  children,
}: {
  children: (overview: RepositoryOverview) => ReactNode;
}) {
  const { overview } = useWorkspace();
  if (overview.status === "success") return <>{children(overview.data)}</>;
  if (overview.status === "error") {
    return (
      <ErrorState
        error={overview.error}
        onRetry={overview.reload}
        context="The repository overview could not be loaded, so this view has no revision to work against."
      />
    );
  }
  return <Loading label="Loading repository overview" />;
}

// ---------- Selection (drives the right-hand intelligence panel) ----------

/**
 * A selection carries only facts the view already received from the API.
 * The panel renders these facts; it never fetches or derives new ones.
 */
export interface Selection {
  kind:
    | "symbol"
    | "module"
    | "file"
    | "finding"
    | "artifact"
    | "session"
    | "evidence";
  title: string;
  subtitle?: string;
  facts: [string, string][];
  symbolId?: string;
  /** First line of the symbol, for opening it in the file viewer. */
  line?: number;
  findingId?: string;
  analysisRunId?: string;
  /** Present when the selection came from the graph view. */
  graphFocus?: { direction: string; depth: number };
  fileId?: string;
  body?: string;
}

interface SelectionValue {
  selection: Selection | null;
  select: (selection: Selection | null) => void;
}

const SelectionContext = createContext<SelectionValue | null>(null);

export function SelectionProvider({
  children,
  onSelect,
}: {
  children: ReactNode;
  /** Called after a non-null selection, e.g. to reveal the panel drawer. */
  onSelect?: () => void;
}) {
  const [selection, setSelection] = useState<Selection | null>(null);
  const value = useMemo(
    () => ({
      selection,
      select: (next: Selection | null) => {
        setSelection(next);
        if (next) onSelect?.();
      },
    }),
    [selection, onSelect],
  );
  return (
    <SelectionContext.Provider value={value}>
      {children}
    </SelectionContext.Provider>
  );
}

export function useSelection(): SelectionValue {
  const value = useContext(SelectionContext);
  if (!value)
    throw new Error("useSelection must be used inside SelectionProvider");
  return value;
}
