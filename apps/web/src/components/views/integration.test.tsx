import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { GraphNeighborhood, PipelineResult } from "@/lib/api/types";
import {
  ingestedOnlyOverview,
  jsonResponse,
  pipelineStatus,
  REPO_ID,
  REV_ID,
  routeFetch,
} from "@/test/fixtures";
import {
  layoutNeighborhood,
  NeighborhoodGraph,
} from "@/components/graph/NeighborhoodGraph";
import { AnalysisPanel } from "@/components/pipeline/AnalysisPanel";
import { contextRefFor } from "@/components/workstation/SaveToSession";
import { RevisionSwitcher } from "@/components/workstation/RevisionSwitcher";
import {
  SelectionProvider,
  WorkspaceProvider,
} from "@/components/workstation/WorkspaceContext";
import { FilesView } from "./FilesView";
import { HomeView } from "./HomeView";
import { ImpactView } from "./ImpactView";
import { PersistedFindings } from "./PersistedLists";
import { QAView } from "./QAView";

const navigation = vi.hoisted(() => ({
  search: "",
  pathname: "",
  push: vi.fn(),
  replace: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(navigation.search),
  usePathname: () => navigation.pathname,
  useRouter: () => ({ push: navigation.push, replace: navigation.replace }),
}));

const FILE_ID = "77777777-7777-4777-8777-777777777777";
const SYMBOL_ID = "88888888-8888-4888-8888-888888888888";

function Workspace({ children }: { children: ReactNode }) {
  return (
    <WorkspaceProvider repositoryId={REPO_ID}>
      <SelectionProvider>{children}</SelectionProvider>
    </WorkspaceProvider>
  );
}

function completedStages() {
  const done = {
    status: "completed" as const,
    up_to_date: true,
    finished_at: "2026-10-01T10:00:00Z",
  };
  return pipelineStatus({ parsing: done, graph: done, rag_chunking: done });
}

function pipelineResult(): PipelineResult {
  return {
    repository_id: REPO_ID,
    revision_id: REV_ID,
    overall_status: "partial",
    execution: "synchronous",
    stages: completedStages().stages.map((stage) => ({
      ...stage,
      requested: true,
      action: stage.stage === "rag_embedding" ? "skipped" : "ran",
      error: null,
    })),
  };
}

beforeEach(() => {
  navigation.search = "";
  navigation.pathname = `/repositories/${REPO_ID}/overview`;
  navigation.push.mockReset();
  navigation.replace.mockReset();
});

describe("ingest then analyze", () => {
  it("separates ingested from analyzed and only analyzes on request", async () => {
    let analyzed = false;
    const fetchMock = routeFetch([
      {
        method: "POST",
        match: "/repositories/ingest",
        respond: () =>
          jsonResponse({
            repository_id: REPO_ID,
            revision_id: REV_ID,
            commit_sha: "abcdef0123456789abcdef0123456789abcdef01",
            status: "ready",
            file_count: 42,
          }),
      },
      {
        method: "POST",
        match: "/analyze",
        respond: () => ((analyzed = true), jsonResponse(pipelineResult())),
      },
      {
        match: "/analysis",
        respond: () =>
          jsonResponse(analyzed ? completedStages() : pipelineStatus()),
      },
    ]);
    render(<HomeView />);

    fireEvent.change(screen.getByLabelText("Public Git repository URL"), {
      target: { value: "https://github.com/acme/billing" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Ingest" }));

    const stages = await screen.findByRole("table", {
      name: "Analysis stages",
    });
    expect(screen.getByText("ingested")).toBeTruthy();
    expect(
      screen.getByText("Analysis (not started by ingestion)"),
    ).toBeTruthy();
    expect(within(stages).getAllByText("Not run")).toHaveLength(3);
    expect(within(stages).getByText("Unavailable")).toBeTruthy();
    expect(
      fetchMock.mock.calls.some(([url]) => String(url).includes("/analyze")),
    ).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "Analyze revision" }));
    expect(await screen.findByText(/Analysis in progress/)).toBeTruthy();
    expect(await screen.findByText("partial")).toBeTruthy();
    const after = screen.getByRole("table", { name: "Analysis stages" });
    expect(within(after).getAllByText("Completed")).toHaveLength(3);
    expect(within(after).getAllByText(/ran now/)).toHaveLength(3);
    expect(within(after).getByText("Unavailable")).toBeTruthy();
    const posts = fetchMock.mock.calls.filter(
      ([, init]) => init?.method === "POST",
    );
    expect(posts.map(([url]) => String(url))).toEqual([
      "http://localhost:8000/repositories/ingest",
      `http://localhost:8000/repositories/${REPO_ID}/revisions/${REV_ID}/analyze`,
    ]);
  });
});

describe("analysis state rendering", () => {
  it("shows outdated distinctly from completed and unavailable distinctly from either", async () => {
    routeFetch([
      {
        match: "/analysis",
        respond: () =>
          jsonResponse(
            pipelineStatus({
              parsing: { status: "completed", up_to_date: true },
              graph: {
                status: "completed",
                up_to_date: false,
                detail:
                  "outdated: an upstream stage was rebuilt after this run",
              },
              rag_chunking: { status: "failed", detail: "boom" },
            }),
          ),
      },
    ]);
    render(<AnalysisPanel repositoryId={REPO_ID} revisionId={REV_ID} />);
    const table = await screen.findByRole("table", { name: "Analysis stages" });
    expect(within(table).getByText("Completed")).toBeTruthy();
    expect(within(table).getByText("Outdated")).toBeTruthy();
    expect(within(table).getByText("Failed")).toBeTruthy();
    expect(within(table).getByText("Unavailable")).toBeTruthy();
    expect(within(table).getByText("boom")).toBeTruthy();
  });

  it("disables analysis while another request holds the revision", async () => {
    routeFetch([
      {
        match: "/analysis",
        respond: () =>
          jsonResponse({ ...pipelineStatus(), analysis_in_progress: true }),
      },
    ]);
    render(<AnalysisPanel repositoryId={REPO_ID} revisionId={REV_ID} />);
    expect(
      await screen.findByText(/Another analysis of this revision is running/),
    ).toBeTruthy();
    expect(
      (
        screen.getByRole("button", {
          name: "Analyze revision",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true);
  });
});

function neighborhood(): GraphNeighborhood {
  const node = (
    id: string,
    name: string,
    depth: number,
    via: GraphNeighborhood["nodes"][number]["reached_via"],
  ) => ({
    symbol_id: id,
    file_id: FILE_ID,
    file_path: "app/m.py",
    qualified_name: name,
    kind: "function",
    depth,
    reached_via: via,
  });
  return {
    revision_id: REV_ID,
    focus_symbol_id: "focus",
    direction: "both",
    depth: 1,
    node_limit: 100,
    nodes: [
      node("focus", "app.m.add", 0, "focus"),
      node("caller", "app.c.compute", 1, "dependents"),
      node("dep", "app.u.helper", 1, "dependencies"),
    ],
    edges: [
      {
        source: "caller",
        target: "focus",
        relationship_type: "calls",
        occurrences: 1,
      },
      {
        source: "focus",
        target: "dep",
        relationship_type: "calls",
        occurrences: 2,
      },
    ],
    truncated: false,
    max_depth_reached: 1,
    focus_external_references: [],
  };
}

describe("real graph edges", () => {
  it("draws exactly the returned edges, dependents left and dependencies right", () => {
    const onSelect = vi.fn();
    const { container } = render(
      <NeighborhoodGraph
        graph={neighborhood()}
        selectedId={null}
        onSelect={onSelect}
      />,
    );
    const edges = [...container.querySelectorAll("[data-edge]")].map((e) =>
      e.getAttribute("data-edge"),
    );
    expect(edges.sort()).toEqual(["caller->focus", "focus->dep"]);

    const placed = Object.fromEntries(
      layoutNeighborhood(neighborhood().nodes).map((n) => [n.symbol_id, n.x]),
    );
    expect(placed.caller).toBeLessThan(placed.focus);
    expect(placed.dep).toBeGreaterThan(placed.focus);

    const node = screen.getByRole("button", {
      name: /app.c.compute, function, dependents at depth 1/,
    });
    fireEvent.keyDown(node, { key: "Enter" });
    expect(onSelect).toHaveBeenCalledWith(
      expect.objectContaining({ symbol_id: "caller" }),
    );
  });

  it("ignores an edge whose endpoint was not returned rather than inventing a node", () => {
    const graph = neighborhood();
    graph.edges.push({
      source: "ghost",
      target: "focus",
      relationship_type: "calls",
      occurrences: 1,
    });
    const { container } = render(
      <NeighborhoodGraph graph={graph} selectedId={null} onSelect={() => {}} />,
    );
    expect(container.querySelectorAll("[data-edge]")).toHaveLength(2);
    expect(screen.getAllByRole("button", { name: /function/ })).toHaveLength(3);
  });
});

describe("files explorer", () => {
  it("lists a directory, opens a file, renders source as text, and selects a symbol", async () => {
    navigation.pathname = `/repositories/${REPO_ID}/files`;
    navigation.search = `file=${FILE_ID}`;
    routeFetch([
      {
        match: "/overview",
        respond: () => jsonResponse(ingestedOnlyOverview()),
      },
      {
        match: "/files?",
        respond: () =>
          jsonResponse({
            directory: "",
            directories: [{ name: "app", path: "app", file_count: 3 }],
            directories_truncated: false,
            files: [],
            total_files: 0,
            limit: 100,
            offset: 0,
          }),
      },
      {
        match: "/source",
        respond: () =>
          jsonResponse({
            file_id: FILE_ID,
            path: "app/m.py",
            start_line: 1,
            end_line: 3,
            total_lines: 3,
            lines: [
              "def add(a, b):",
              "    return '<img src=x onerror=alert(1)>'",
              "# <script>alert(1)</script>",
            ],
            truncated: false,
          }),
      },
      {
        match: `/files/${FILE_ID}`,
        respond: () =>
          jsonResponse({
            file: {
              file_id: FILE_ID,
              path: "app/m.py",
              language: "python",
              size_bytes: 40,
              line_count: 2,
              parsing_status: "parsed",
            },
            revision_id: REV_ID,
            symbols: [
              {
                symbol_id: SYMBOL_ID,
                name: "add",
                qualified_name: "app.m.add",
                kind: "function",
                file_id: FILE_ID,
                file_path: "app/m.py",
                start_line: 1,
                end_line: 2,
                parent_symbol_id: null,
                signature: "(a, b)",
              },
            ],
            symbols_truncated: false,
            source_available: true,
          }),
      },
    ]);
    const { container } = render(
      <Workspace>
        <FilesView />
      </Workspace>,
    );

    expect(
      await screen.findByRole("button", { name: /app\/\s*3 files/ }),
    ).toBeTruthy();
    const source = await screen.findByRole("region", {
      name: "Source of app/m.py",
    });
    expect(within(source).getByText("def add(a, b):")).toBeTruthy();
    // Untrusted content stays text: no element was created from it.
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(within(source).getByText(/onerror=alert/)).toBeTruthy();
    expect(
      within(source).getByText("# <script>alert(1)</script>"),
    ).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /function add :1/ }));
    expect(navigation.replace).toHaveBeenCalledWith(
      expect.stringContaining("line=1"),
      { scroll: false },
    );
    await waitFor(() =>
      expect(container.querySelectorAll("tr[data-line]")[0].className).not.toBe(
        "",
      ),
    );
  });
});

describe("revision switching", () => {
  it("lists revisions on demand and keeps the current section", async () => {
    navigation.pathname = `/repositories/${REPO_ID}/architecture`;
    const fetchMock = routeFetch([
      {
        match: "/overview",
        respond: () => jsonResponse(ingestedOnlyOverview()),
      },
      {
        match: "/revisions?",
        respond: () =>
          jsonResponse({
            revisions: [
              {
                revision_id: "99999999-9999-4999-8999-999999999999",
                commit_sha: "1234567890abcdef1234567890abcdef12345678",
                parent_sha: null,
                branch_ref: null,
                committed_at: "2026-09-01T00:00:00Z",
                discovered_at: "2026-09-01T00:00:00Z",
                latest_analysis_status: "completed",
                file_count: 10,
                is_current: false,
              },
            ],
            total_count: 1,
            limit: 50,
            offset: 0,
          }),
      },
    ]);
    const { container } = render(
      <Workspace>
        <RevisionSwitcher currentRevisionId={REV_ID} />
      </Workspace>,
    );
    expect(
      fetchMock.mock.calls.some(([url]) => String(url).includes("/revisions?")),
    ).toBe(false);
    const details = container.querySelector("details")!;
    details.open = true;
    fireEvent(details, new Event("toggle"));
    const link = await screen.findByRole("link", { name: /1234567890/ });
    expect(link.getAttribute("href")).toBe(
      `/repositories/${REPO_ID}/architecture?revision=99999999-9999-4999-8999-999999999999`,
    );
  });
});

describe("Q&A index state", () => {
  it("says indexing has not completed instead of answering without evidence", async () => {
    navigation.pathname = `/repositories/${REPO_ID}/qa`;
    routeFetch([
      {
        match: "/overview",
        respond: () => jsonResponse(ingestedOnlyOverview()),
      },
      { match: "/analysis", respond: () => jsonResponse(pipelineStatus()) },
    ]);
    render(
      <Workspace>
        <QAView />
      </Workspace>,
    );
    expect(
      await screen.findByText(
        /Codebase indexing has not completed for this revision/,
      ),
    ).toBeTruthy();
  });
});

describe("persisted findings", () => {
  it("distinguishes no completed scan from a clean scan", async () => {
    routeFetch([
      {
        match: "/overview",
        respond: () => jsonResponse(ingestedOnlyOverview()),
      },
      {
        match: "/security/findings",
        respond: () =>
          jsonResponse({
            findings_run_id: null,
            findings: [],
            total_count: 0,
            limit: 50,
            offset: 0,
          }),
      },
    ]);
    render(
      <Workspace>
        <PersistedFindings revisionId={REV_ID} />
      </Workspace>,
    );
    expect(
      await screen.findByText(/No completed security scan for this revision/),
    ).toBeTruthy();
  });
});

describe("session context mapping", () => {
  it("maps each selection to the backend reference type that can hold it", () => {
    const base = { kind: "symbol" as const, title: "x", facts: [] };
    expect(
      contextRefFor(
        { ...base, symbolId: "s", graphFocus: { direction: "both", depth: 2 } },
        REV_ID,
      ),
    ).toEqual({
      ref_type: "graph_focus",
      graph_camera_state: {
        focus_symbol_id: "s",
        direction: "both",
        depth: 2,
        revision_id: REV_ID,
      },
    });
    expect(
      contextRefFor({ ...base, symbolId: "s", fileId: "f" }, REV_ID),
    ).toEqual({ ref_type: "selected_symbol", symbol_id: "s" });
    expect(
      contextRefFor({ ...base, kind: "file", fileId: "f" }, REV_ID),
    ).toEqual({ ref_type: "selected_file", file_id: "f" });
    expect(
      contextRefFor({ ...base, kind: "finding", findingId: "g" }, REV_ID),
    ).toEqual({ ref_type: "finding_reference", finding_id: "g" });
    expect(contextRefFor({ ...base, kind: "evidence" }, REV_ID)).toBeNull();
  });
});

describe("impact from a link", () => {
  it("pre-fills a revision-scoped target and only analyzes on request", async () => {
    navigation.pathname = `/repositories/${REPO_ID}/impact`;
    navigation.search = `symbol=${SYMBOL_ID}`;
    const fetchMock = routeFetch([
      {
        match: "/overview",
        respond: () => jsonResponse(ingestedOnlyOverview()),
      },
      {
        method: "POST",
        match: "/impact",
        respond: () =>
          jsonResponse({
            revision_id: REV_ID,
            target_symbol_id: SYMBOL_ID,
            target_qualified_name: "app.m.add",
            direct_impacts: [],
            transitive_impacts: [],
            impacted_files: [],
            max_depth: 0,
            analysis_run_id: null,
            analysis_status: "deterministic",
            explanation: null,
            evidence: null,
          }),
      },
    ]);
    render(
      <Workspace>
        <ImpactView />
      </Workspace>,
    );
    const button = await screen.findByRole("button", {
      name: "Analyze this target",
    });
    expect(
      fetchMock.mock.calls.some(([, init]) => init?.method === "POST"),
    ).toBe(false);

    fireEvent.click(button);
    expect(
      await screen.findByText("Nothing depends on this symbol"),
    ).toBeTruthy();
    const post = fetchMock.mock.calls.find(
      ([, init]) => init?.method === "POST",
    )!;
    expect(JSON.parse(String(post[1]?.body))).toEqual({
      revision_id: REV_ID,
      include_explanation: false,
      symbol_id: SYMBOL_ID,
    });
  });
});
