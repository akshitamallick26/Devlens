import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { GraphNeighborhood } from "@/lib/api/types";
import {
  ingestedOnlyOverview,
  jsonResponse,
  REPO_ID,
  REV_ID,
  routeFetch,
} from "@/test/fixtures";
import { Workstation } from "@/components/workstation/Workstation";
import { useSelection } from "@/components/workstation/WorkspaceContext";
import { ArchitectureView } from "./ArchitectureView";
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

const FOCUS = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const CALLER = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const DEP = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";

function analyzedOverview() {
  const overview = ingestedOnlyOverview();
  overview.analysis_statuses = overview.analysis_statuses.map((s) =>
    s.analysis_type === "graph_build" || s.analysis_type === "parsing"
      ? { ...s, status: "completed" as const }
      : s,
  );
  return overview;
}

function neighborhood(): GraphNeighborhood {
  const node = (
    id: string,
    name: string,
    depth: number,
    via: GraphNeighborhood["nodes"][number]["reached_via"],
  ) => ({
    symbol_id: id,
    file_id: "f",
    file_path: "app/m.py",
    qualified_name: name,
    kind: "function",
    depth,
    reached_via: via,
  });
  return {
    revision_id: REV_ID,
    focus_symbol_id: FOCUS,
    direction: "dependencies",
    depth: 3,
    node_limit: 50,
    nodes: [
      node(FOCUS, "app.m.add", 0, "focus"),
      node(CALLER, "app.c.compute", 1, "dependents"),
      node(DEP, "app.u.helper", 1, "dependencies"),
    ],
    edges: [
      {
        source: CALLER,
        target: FOCUS,
        relationship_type: "calls",
        occurrences: 1,
      },
      {
        source: FOCUS,
        target: DEP,
        relationship_type: "imports",
        occurrences: 1,
      },
    ],
    truncated: false,
    max_depth_reached: 1,
    focus_external_references: [],
  };
}

beforeEach(() => {
  navigation.search = "";
  navigation.pathname = `/repositories/${REPO_ID}/overview`;
  navigation.replace.mockReset();
  window.matchMedia = vi
    .fn()
    .mockReturnValue({ matches: false }) as unknown as typeof window.matchMedia;
});

describe("architecture graph state", () => {
  it("restores the exact bounded graph request from the URL and lists the same edges as the SVG", async () => {
    navigation.pathname = `/repositories/${REPO_ID}/architecture`;
    navigation.search = `focus=${FOCUS}&direction=dependencies&depth=3&limit=50`;
    const fetchMock = routeFetch([
      { match: "/overview", respond: () => jsonResponse(analyzedOverview()) },
      {
        match: "/graph/neighborhood",
        respond: () => jsonResponse(neighborhood()),
      },
    ]);
    const { container } = render(
      <Workstation repositoryId={REPO_ID}>
        <ArchitectureView />
      </Workstation>,
    );
    await screen.findByRole("group", {
      name: /Dependency graph neighbourhood/,
    });

    const graphCall = fetchMock.mock.calls.find(([url]) =>
      String(url).includes("/graph/neighborhood"),
    )!;
    const url = new URL(String(graphCall[0]));
    expect(graphCall[1]?.method ?? "GET").toBe("GET");
    expect(Object.fromEntries(url.searchParams)).toEqual({
      symbol_id: FOCUS,
      direction: "dependencies",
      depth: "3",
      limit: "50",
    });

    const svgEdges = [...container.querySelectorAll("[data-edge]")]
      .map((e) => e.getAttribute("data-edge"))
      .sort();
    const rows = within(screen.getByRole("table")).getAllByRole("row").slice(1);
    const tableEdges = rows
      .map((row) => {
        const [source, , target] = within(row)
          .getAllByRole("cell")
          .map((c) => c.textContent);
        const id = (name: string | null) =>
          neighborhood().nodes.find((n) => n.qualified_name === name)!
            .symbol_id;
        return `${id(source)}->${id(target)}`;
      })
      .sort();
    expect(tableEdges).toEqual(svgEdges);
    expect(svgEdges).toEqual(
      [`${CALLER}->${FOCUS}`, `${FOCUS}->${DEP}`].sort(),
    );
  });

  it("clamps out-of-range URL bounds before requesting", async () => {
    navigation.pathname = `/repositories/${REPO_ID}/architecture`;
    navigation.search = `focus=${FOCUS}&depth=99&limit=100000&direction=sideways`;
    const fetchMock = routeFetch([
      { match: "/overview", respond: () => jsonResponse(analyzedOverview()) },
      {
        match: "/graph/neighborhood",
        respond: () => jsonResponse(neighborhood()),
      },
    ]);
    render(
      <Workstation repositoryId={REPO_ID}>
        <ArchitectureView />
      </Workstation>,
    );
    await screen.findByRole("group", {
      name: /Dependency graph neighbourhood/,
    });
    const url = new URL(
      String(
        fetchMock.mock.calls.find(([u]) => String(u).includes("/graph/"))![0],
      ),
    );
    expect(url.searchParams.get("depth")).toBe("4");
    expect(url.searchParams.get("limit")).toBe("100");
    expect(url.searchParams.get("direction")).toBe("both");
  });
});

describe("Q&A from a link", () => {
  it("pre-fills the question and only POSTs after an explicit Ask", async () => {
    navigation.pathname = `/repositories/${REPO_ID}/qa`;
    navigation.search = "q=where%20are%20refunds%20posted";
    const fetchMock = routeFetch([
      { match: "/overview", respond: () => jsonResponse(analyzedOverview()) },
      {
        match: "/analysis",
        respond: () =>
          jsonResponse({
            repository_id: REPO_ID,
            revision_id: REV_ID,
            stages: [],
            analysis_in_progress: false,
          }),
      },
      {
        method: "POST",
        match: "/qa",
        respond: () =>
          jsonResponse({
            revision_id: REV_ID,
            question: "where are refunds posted",
            analysis_status: "retrieval_only",
            evidence_items: [],
            answer: null,
            evidence: null,
          }),
      },
    ]);
    render(
      <Workstation repositoryId={REPO_ID}>
        <QAView />
      </Workstation>,
    );
    expect(await screen.findByText("Ready to ask")).toBeTruthy();
    expect((screen.getByLabelText("Question") as HTMLInputElement).value).toBe(
      "where are refunds posted",
    );
    expect(
      fetchMock.mock.calls.some(([, init]) => init?.method === "POST"),
    ).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "Ask" }));
    await screen.findByText("No evidence retrieved");
    const post = fetchMock.mock.calls.find(
      ([, init]) => init?.method === "POST",
    )!;
    expect(JSON.parse(String(post[1]?.body))).toEqual({
      revision_id: REV_ID,
      question: "where are refunds posted",
    });
  });
});

function Selector() {
  const { select } = useSelection();
  return (
    <button
      type="button"
      onClick={() =>
        select({
          kind: "symbol",
          title: "app.m.add",
          facts: [],
          symbolId: FOCUS,
        })
      }
    >
      pick
    </button>
  );
}

describe("revision switching", () => {
  it("drops a selection made in another revision", async () => {
    routeFetch([
      {
        match: "/overview",
        respond: () => jsonResponse(ingestedOnlyOverview()),
      },
    ]);
    const view = (
      <Workstation repositoryId={REPO_ID}>
        <Selector />
      </Workstation>
    );
    const { rerender } = render(view);
    fireEvent.click(screen.getByRole("button", { name: "pick" }));
    const panel = screen.getByRole("complementary", {
      name: "Selection details",
    });
    expect(within(panel).getByText("app.m.add")).toBeTruthy();

    navigation.search = "revision=99999999-9999-4999-8999-999999999999";
    rerender(
      <Workstation repositoryId={REPO_ID}>
        <Selector />
      </Workstation>,
    );
    // The provider remounts on a revision change, so query the live panel.
    await waitFor(() =>
      expect(
        within(
          screen.getByRole("complementary", { name: "Selection details" }),
        ).getByText(/Nothing selected/),
      ).toBeTruthy(),
    );
    expect(screen.queryByText("app.m.add")).toBeNull();
  });
});
