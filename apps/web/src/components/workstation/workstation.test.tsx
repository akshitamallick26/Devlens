import { render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  ingestedOnlyOverview,
  jsonResponse,
  pipelineStatus,
  REPO_ID,
  routeFetch,
  stubFetch,
} from "@/test/fixtures";
import { OverviewView } from "@/components/views/OverviewView";
import { Workstation } from "./Workstation";
import { parseCommand } from "./commands";

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

function renderOverviewRoute() {
  return render(
    <Workstation repositoryId={REPO_ID}>
      <OverviewView />
    </Workstation>,
  );
}

beforeEach(() => {
  navigation.search = "";
  navigation.pathname = `/repositories/${REPO_ID}/overview`;
  window.matchMedia = vi
    .fn()
    .mockReturnValue({ matches: false }) as unknown as typeof window.matchMedia;
});

describe("repository route", () => {
  it("renders the shell and overview from the overview endpoint", async () => {
    const fetchMock = routeFetch([
      {
        match: "/overview",
        respond: () => jsonResponse(ingestedOnlyOverview()),
      },
      { match: "/analysis", respond: () => jsonResponse(pipelineStatus()) },
    ]);
    renderOverviewRoute();

    expect(
      await screen.findByRole("heading", { level: 1, name: "acme/billing" }),
    ).toBeTruthy();
    await screen.findByRole("table", { name: "Analysis stages" });
    const calls = fetchMock.mock.calls.map(([url, init]) => [
      init?.method ?? "GET",
      String(url),
    ]);
    // The overview is fetched once and shared; page load never POSTs (no
    // analysis run is created by viewing a page).
    expect(calls.filter(([, url]) => url.endsWith("/overview"))).toEqual([
      ["GET", `http://localhost:8000/repositories/${REPO_ID}/overview`],
    ]);
    expect(calls.every(([method]) => method === "GET")).toBe(true);

    const nav = screen.getByRole("navigation", { name: "Workstation" });
    const overviewLink = within(nav).getByRole("link", { name: "Overview" });
    expect(overviewLink.getAttribute("aria-current")).toBe("page");
    expect(
      within(nav).getByRole("link", { name: "Why do refunds double-post?" }),
    ).toBeTruthy();
  });

  it("keeps an explicit revision in the request and in navigation links", async () => {
    navigation.search = "revision=99999999-9999-4999-8999-999999999999";
    const fetchMock = stubFetch(() => jsonResponse(ingestedOnlyOverview()));
    renderOverviewRoute();
    await screen.findByRole("heading", { level: 1, name: "acme/billing" });
    expect(String(fetchMock.mock.calls[0][0])).toContain(
      "revision_id=99999999-9999-4999-8999-999999999999",
    );
    const impactLink = screen.getByRole("link", { name: "Impact analysis" });
    expect(impactLink.getAttribute("href")).toBe(
      `/repositories/${REPO_ID}/impact?revision=99999999-9999-4999-8999-999999999999`,
    );
  });

  it("shows not-run stages as not run, never as zero", async () => {
    stubFetch(() => jsonResponse(ingestedOnlyOverview()));
    renderOverviewRoute();
    const sizes = await screen.findByLabelText("Repository size");

    expect(within(sizes).getAllByText("not parsed").length).toBe(2);
    expect(within(sizes).getAllByText("graph not built").length).toBe(2);
    expect(within(sizes).queryByText("0")).toBeNull();
    expect(within(sizes).getByText("42")).toBeTruthy();

    expect(
      screen.getByText(
        "No security scan has run for this revision. No finding counts exist.",
      ),
    ).toBeTruthy();
    expect(screen.getAllByText("Not run").length).toBeGreaterThan(5);
    expect(screen.queryByText(/Completed/)).toBeTruthy(); // ingestion only
  });

  it("shows recorded-vs-missing automation counts truthfully", async () => {
    const overview = ingestedOnlyOverview();
    overview.automation = {
      ...overview.automation,
      latest_run: {
        ...overview.automation.latest_run,
        status: "completed",
        analysis_run_id: "r",
      },
      changed_file_count: 0,
      changed_symbol_count: null,
    };
    stubFetch(() => jsonResponse(overview));
    renderOverviewRoute();
    await screen.findByRole("heading", { level: 1, name: "acme/billing" });
    const section = screen
      .getByRole("heading", { name: "Change analysis" })
      .closest("section")!;
    expect(within(section).getByText("0")).toBeTruthy();
    expect(
      within(section).getAllByText("not recorded").length,
    ).toBeGreaterThanOrEqual(1);
  });

  it("explains a backend outage instead of rendering intelligence", async () => {
    stubFetch(() => {
      throw new TypeError("Failed to fetch");
    });
    renderOverviewRoute();
    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText("Backend unavailable")).toBeTruthy();
    expect(screen.queryByRole("heading", { level: 1 })).toBeNull();
  });

  it("treats a malformed overview body as an error, not as data", async () => {
    stubFetch(() => jsonResponse({ repository: { repository_id: REPO_ID } }));
    renderOverviewRoute();
    const alert = await screen.findByRole("alert");
    expect(
      within(alert).getByText("Unexpected response from the API"),
    ).toBeTruthy();
  });

  it("maps an unknown repository to a not-found state", async () => {
    stubFetch(() => jsonResponse({ detail: `no repository ${REPO_ID}` }, 404));
    renderOverviewRoute();
    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText("Not found")).toBeTruthy();
    await waitFor(() =>
      expect(screen.getByText("Repository unavailable")).toBeTruthy(),
    );
  });
});

describe("command parsing", () => {
  it("routes only to supported capabilities", () => {
    expect(parseCommand("  ")).toEqual({ kind: "empty" });
    expect(parseCommand("help")).toEqual({ kind: "help" });
    expect(parseCommand("impact billing.refunds.post")).toEqual({
      kind: "impact",
      qualifiedName: "billing.refunds.post",
    });
    expect(parseCommand("ask where are refunds posted?")).toEqual({
      kind: "ask",
      question: "where are refunds posted?",
    });
    expect(parseCommand("Why is this function affected?")).toEqual({
      kind: "ask",
      question: "Why is this function affected?",
    });
  });
});
