import { fireEvent, render, screen, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type {
  ContextRef,
  ImpactResponse,
  SessionSummary,
} from "@/lib/api/types";
import {
  ingestedOnlyOverview,
  jsonResponse,
  REPO_ID,
  stubFetch,
} from "@/test/fixtures";
import {
  layoutRings,
  MAX_RENDERED_NODES,
} from "@/components/graph/DepthRingGraph";
import {
  SelectionProvider,
  WorkspaceProvider,
} from "@/components/workstation/WorkspaceContext";
import { DeletedSymbolRow } from "./AutomationView";
import { GenerationBadge } from "./DocumentationView";
import { ImpactResult } from "./ImpactResult";
import { ScannerOutcome } from "./SecurityView";
import { ContextRefRow, SessionList } from "./SessionsView";

vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(""),
  usePathname: () => "",
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

function Providers({ children }: { children: ReactNode }) {
  return (
    <WorkspaceProvider repositoryId={REPO_ID}>
      <SelectionProvider>{children}</SelectionProvider>
    </WorkspaceProvider>
  );
}

beforeEach(() => {
  stubFetch(() => jsonResponse(ingestedOnlyOverview()));
});

function impact(overrides: Partial<ImpactResponse> = {}): ImpactResponse {
  const symbol = (id: string, name: string, depth: number) => ({
    symbol_id: id,
    qualified_name: name,
    symbol_kind: "function",
    file_id: `file-${id}`,
    file_path: `billing/${id}.py`,
    depth,
    relationship_types: ["calls"],
    parent_symbol_id: null,
  });
  return {
    revision_id: "rev",
    target_symbol_id: "target",
    target_qualified_name: "billing.ledger.post_entry",
    direct_impacts: [symbol("a", "billing.refunds.refund", 1)],
    transitive_impacts: [symbol("b", "billing.api.refund_endpoint", 2)],
    impacted_files: [
      {
        file_id: "file-a",
        path: "billing/a.py",
        symbol_count: 1,
        minimum_depth: 1,
      },
    ],
    max_depth: 2,
    analysis_run_id: "run-1",
    analysis_status: "deterministic",
    explanation: null,
    evidence: null,
    ...overrides,
  };
}

describe("impact facts vs explanation", () => {
  it("labels deterministic facts and shows no explanation when none was requested", () => {
    render(
      <Providers>
        <ImpactResult result={impact()} />
      </Providers>,
    );
    expect(screen.getByText("Deterministic facts")).toBeTruthy();
    expect(screen.queryByText("AI explanation")).toBeNull();
    expect(
      screen.getAllByText("billing.refunds.refund").length,
    ).toBeGreaterThan(0);
  });

  it("renders the explanation in a separately labelled block with inspectable evidence", () => {
    const result = impact({
      analysis_status: "explained",
      explanation: {
        summary: "Refund posting depends on the ledger.",
        why_it_matters: "Ledger changes alter refunds.",
        affected_areas: ["refunds"],
        review_recommendations: [],
        test_recommendations: [],
        evidence: ["E1", "E9"],
        limitations: "Static analysis only.",
      },
      evidence: {
        E1: { file_path: "billing/a.py", line_start: 3, line_end: 9 },
      },
    });
    render(
      <Providers>
        <ImpactResult result={result} />
      </Providers>,
    );
    const block = screen.getByText("AI explanation").closest("section")!;
    expect(
      within(block).getByText("Refund posting depends on the ledger."),
    ).toBeTruthy();
    expect(within(block).getByText("billing/a.py:3-9")).toBeTruthy();
    expect(
      within(block).getByText("location not resolved by the backend"),
    ).toBeTruthy();
    // Facts live outside the explanation block.
    expect(within(block).queryByText("Deterministic facts")).toBeNull();
  });

  it("says when a requested explanation was unavailable", () => {
    render(
      <Providers>
        <ImpactResult
          result={impact({ analysis_status: "deterministic_only" })}
        />
      </Providers>,
    );
    expect(
      screen.getByText(/explanation was requested but is unavailable/),
    ).toBeTruthy();
  });

  it("reports an empty dependent set as such", () => {
    render(
      <Providers>
        <ImpactResult
          result={impact({
            direct_impacts: [],
            transitive_impacts: [],
            impacted_files: [],
            max_depth: 0,
          })}
        />
      </Providers>,
    );
    expect(screen.getByText("Nothing depends on this symbol")).toBeTruthy();
  });
});

describe("security scanner outcomes", () => {
  const base = { detail: null, observation_count: 0, scanner_version: null };

  it("distinguishes an unavailable scanner from a clean scan", () => {
    render(
      <ul>
        <ScannerOutcome
          result={{
            ...base,
            scanner: "semgrep",
            status: "unavailable",
            finding_count: 0,
          }}
        />
        <ScannerOutcome
          result={{
            ...base,
            scanner: "osv-scanner",
            status: "completed",
            finding_count: 0,
          }}
        />
      </ul>,
    );
    expect(screen.getByText("Scanner unavailable")).toBeTruthy();
    expect(screen.getByText(/this is not a clean result/)).toBeTruthy();
    expect(screen.getByText("Scanned")).toBeTruthy();
    expect(screen.getByText("Scan completed with no findings.")).toBeTruthy();
  });
});

describe("automation deleted symbols", () => {
  it("never presents unavailable impact as no impact", () => {
    render(
      <ul>
        <DeletedSymbolRow
          item={{
            file_path: "billing/old.py",
            kind: "function",
            qualified_name: "billing.old.legacy_refund",
            symbol_id: "s",
            impact_status: "unavailable_deleted_symbol",
            reason:
              "The symbol no longer exists in the current revision's graph.",
          }}
        />
      </ul>,
    );
    expect(
      screen.getByText("impact unavailable (symbol deleted)"),
    ).toBeTruthy();
    expect(screen.getByText(/no longer exists/)).toBeTruthy();
  });
});

describe("documentation provenance", () => {
  it("labels fallback and model output differently", () => {
    render(
      <>
        <GenerationBadge status="fallback" />
        <GenerationBadge status="generated" />
      </>,
    );
    expect(screen.getByText("Deterministic fallback")).toBeTruthy();
    expect(screen.getByText("LLM generated")).toBeTruthy();
  });
});

describe("sessions", () => {
  const session = (
    id: string,
    title: string | null,
    last: string,
  ): SessionSummary => ({
    id,
    title,
    status: "active",
    started_at_revision_id: null,
    current_revision_id: null,
    created_at: last,
    last_active_at: last,
    message_count: 2,
  });

  it("keeps backend recency order and labels untitled sessions", () => {
    render(
      <Providers>
        <SessionList
          sessions={[
            session("s2", "Newest", "2026-10-03T00:00:00Z"),
            session("s1", null, "2026-10-01T00:00:00Z"),
          ]}
        />
      </Providers>,
    );
    const links = screen.getAllByRole("link").map((a) => a.textContent);
    expect(links).toEqual(["Newest", "Untitled session"]);
  });

  it("restores only what the backend can identify", () => {
    const ref = (ref_type: string, extra: Partial<ContextRef>): ContextRef => ({
      id: ref_type,
      session_id: "s",
      message_id: null,
      ref_type,
      file_id: null,
      symbol_id: null,
      analysis_run_id: null,
      finding_id: null,
      graph_camera_state: null,
      created_at: "2026-10-01T00:00:00Z",
      ...extra,
    });
    render(
      <Providers>
        <ul>
          <ContextRefRow
            contextRef={ref("selected_symbol", {
              symbol_id: "55555555-5555-4555-8555-555555555555",
            })}
          />
          <ContextRefRow
            contextRef={ref("finding_reference", {
              finding_id: "66666666-6666-4666-8666-666666666666",
            })}
          />
        </ul>
      </Providers>,
    );
    const link = screen.getByRole("link", { name: "Open impact for symbol" });
    expect(link.getAttribute("href")).toBe(
      `/repositories/${REPO_ID}/impact?symbol=55555555-5555-4555-8555-555555555555`,
    );
    expect(
      screen.getByText("Recorded; this workstation cannot restore it."),
    ).toBeTruthy();
  });
});

describe("depth ring layout", () => {
  it("is deterministic and places nodes on their depth ring", () => {
    const nodes = [
      { id: "a", label: "x.a", depth: 1 },
      { id: "b", label: "x.b", depth: 1 },
      { id: "c", label: "x.c", depth: 2 },
    ];
    const first = layoutRings(nodes);
    expect(layoutRings(nodes)).toEqual(first);
    expect(first.maxDepth).toBe(2);
    const radius = (id: string) => {
      const n = first.placed.find((p) => p.id === id)!;
      return Math.round(Math.hypot(n.x, n.y));
    };
    expect(radius("a")).toBe(radius("b"));
    expect(radius("c")).toBe(radius("a") * 2);
  });

  it("caps rendered nodes and keeps the full list in the tables", () => {
    const many = Array.from({ length: MAX_RENDERED_NODES + 25 }, (_, i) => ({
      symbol_id: `s${i}`,
      qualified_name: `pkg.mod.f${i}`,
      symbol_kind: "function",
      file_id: "f",
      file_path: "pkg/mod.py",
      depth: 1,
      relationship_types: ["calls"],
      parent_symbol_id: null,
    }));
    render(
      <Providers>
        <ImpactResult
          result={impact({ direct_impacts: many, transitive_impacts: [] })}
        />
      </Providers>,
    );
    const graph = screen.getByRole("group", { name: /Dependents of/ });
    expect(within(graph).getAllByRole("button").length).toBe(
      MAX_RENDERED_NODES,
    );
    expect(
      screen.getByText(/Showing the first 150 of 175 dependents/),
    ).toBeTruthy();
    // Bounded table rendering with explicit expansion.
    fireEvent.click(screen.getByRole("button", { name: /Show 75 more of 75/ }));
    expect(screen.queryByRole("button", { name: /more of/ })).toBeNull();
  });
});
