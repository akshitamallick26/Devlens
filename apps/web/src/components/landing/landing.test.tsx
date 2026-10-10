import { render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { graphData, impactDepths } from "./data/graph";
import { LandingPage } from "./LandingPage";
import { LandingWorld } from "./LandingWorld";
import { separateLabels } from "./world/labelLayout";
import {
  AUTOMATION_SEQUENCE,
  SCENES,
  emptySceneState,
  packageWeights,
  sceneState,
} from "./world/timeline";

function stubMedia(reduced: boolean) {
  vi.stubGlobal(
    "matchMedia",
    (query: string) =>
      ({
        matches: reduced && query.includes("prefers-reduced-motion"),
        media: query,
        addEventListener: () => undefined,
        removeEventListener: () => undefined,
      }) as unknown as MediaQueryList,
  );
}

describe("landing page content", () => {
  it("has one h1, the narrative headings in order, and every scene", () => {
    const { container } = render(<LandingPage />);
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe(
      "Understand your codebefore you change it.",
    );
    const headings = screen
      .getAllByRole("heading", { level: 2 })
      .map((h) => h.textContent);
    expect(headings).toEqual([
      "It starts as text.",
      "Your code already has a shape.",
      "DevLens maps the relationships hiding inside your repository.",
      "Change one function.See what breaks before you touch it.",
      "AI explains the evidence.It doesn't invent the graph.",
      "Tests and findings, tied to the same graph.",
      "Every revision becomes intelligence.",
      "One pipeline, one revision at a time.",
      "Understand before you change.",
    ]);
    const scenes = Array.from(container.querySelectorAll("[data-scene]")).map(
      (el) => el.getAttribute("data-scene"),
    );
    expect(scenes).toEqual([...SCENES]);
  });

  it("links the CTAs to the real workspace and the on-page architecture", () => {
    render(<LandingPage />);
    for (const link of screen.getAllByRole("link", {
      name: "Explore DevLens",
    })) {
      expect(link.getAttribute("href")).toBe("/workspace");
    }
    for (const link of screen.getAllByRole("link", {
      name: "View the architecture",
    })) {
      expect(link.getAttribute("href")).toBe("/#architecture");
    }
    expect(
      screen.getByRole("link", { name: "Open workspace" }).getAttribute("href"),
    ).toBe("/workspace");
    expect(document.getElementById("architecture")).not.toBeNull();
  });

  it("states real DevLens output, not invented figures", () => {
    render(<LandingPage />);
    const impact = screen.getByRole("region", { name: /Change one function/ });
    const listed = within(impact)
      .getAllByRole("listitem")
      .filter((li) => li.querySelector("code") && !li.querySelector("ul"));
    const dependents = graphData.impactedSymbols.filter((s) => s.depth > 0);
    expect(listed).toHaveLength(dependents.length);
    expect(impact.textContent).toContain("load_lines_for_files");
    const structure = screen.getByRole("region", {
      name: /already has a shape/,
    });
    for (const count of Object.values(
      graphData.provenance.edgesByRelationship,
    )) {
      expect(structure.textContent).toContain(count.toLocaleString("en-US"));
    }
  });

  it("keeps the copy free of em dashes and marketing filler", () => {
    const { container } = render(<LandingPage />);
    const text = container.textContent ?? "";
    expect(text).not.toContain("—");
    for (const phrase of [
      "revolutionary",
      "supercharge",
      "10x",
      "next-generation",
      "unlock the future",
    ]) {
      expect(text.toLowerCase()).not.toContain(phrase);
    }
  });
});

describe("landing world", () => {
  it("uses the static graph under reduced motion, calls no API, and cleans up", async () => {
    stubMedia(true);
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    vi.spyOn(console, "error").mockImplementation(() => undefined); // jsdom: canvas not implemented
    const { container, unmount } = render(<LandingWorld />);
    const host = container.querySelector("[data-mode]")!;
    expect(host.getAttribute("aria-hidden")).toBe("true");
    await waitFor(() => expect(host.getAttribute("data-mode")).toBe("static"));
    expect(host.querySelectorAll("canvas")).toHaveLength(1);
    unmount();
    expect(container.querySelectorAll("canvas")).toHaveLength(0);
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});

describe("timeline", () => {
  const maxDepth = graphData.provenance.impactMaxDepth;
  const at = (t: number) => sceneState(t, maxDepth, emptySceneState());

  it("moves from code to graph to impact in order", () => {
    expect(at(0.2)).toMatchObject({ codeOpacity: 0, graphOpacity: 0 });
    expect(at(1.6).codeOpacity).toBe(1);
    expect(at(3.5)).toMatchObject({
      graphOpacity: 1,
      impactMix: 0,
      codeOpacity: 0,
    });
    const impact = at(4.95);
    expect(impact.impactMix).toBe(1);
    expect(impact.impactFront).toBeGreaterThanOrEqual(maxDepth);
    expect(at(6.5)).toMatchObject({ impactMix: 0, packageMix: 1 });
  });

  it("emphasizes the real testing and security packages, then automation's stage order", () => {
    const packages = graphData.packages;
    const weights = new Float32Array(packages.length);
    packageWeights(6.5, packages, weights);
    const lit = packages.filter((_, i) => weights[i] === 1);
    expect(lit.sort()).toEqual(["security", "testing"]);
    packageWeights(7.95, packages, weights);
    for (const name of AUTOMATION_SEQUENCE) {
      expect(packages).toContain(name);
      expect(weights[packages.indexOf(name)]).toBe(1);
    }
  });
});

describe("exported graph data", () => {
  it("is internally consistent", () => {
    const n = graphData.names.length;
    expect(graphData.provenance.symbols).toBe(n);
    expect(graphData.positions).toHaveLength(n * 3);
    expect(graphData.edges.length / 3).toBe(graphData.provenance.edges);
    const byRel = Object.values(
      graphData.provenance.edgesByRelationship,
    ).reduce((a, b) => a + b, 0);
    expect(byRel).toBe(graphData.provenance.edges);
    for (let e = 0; e < graphData.edges.length; e += 3) {
      expect(graphData.edges[e]).toBeLessThan(n);
      expect(graphData.edges[e + 1]).toBeLessThan(n);
    }
    const depths = impactDepths(graphData);
    expect(depths[graphData.impact.seed]).toBe(0);
    expect(graphData.names[graphData.impact.seed]).toBe(
      graphData.provenance.impactSeed,
    );
    for (const symbol of graphData.impactedSymbols) {
      expect(depths[graphData.names.indexOf(symbol.name)]).toBe(symbol.depth);
    }
  });
});

describe("package label layout", () => {
  it("pushes a colliding label below the one above it, leaves clear labels alone", () => {
    const rects = [
      { x: 100, y: 200, width: 120, height: 28 }, // "investigation"
      { x: 110, y: 210, width: 90, height: 28 }, // "security", overlapping
      { x: 600, y: 205, width: 80, height: 28 }, // far to the right: untouched
    ];
    const shifts = separateLabels(rects, 6);
    expect(shifts[0]).toBe(0);
    expect(shifts[1]).toBe(200 + 28 + 6 - 210);
    expect(shifts[2]).toBe(0);
  });

  it("is deterministic and resolves chains of collisions", () => {
    const stacked = [0, 1, 2, 3].map((i) => ({
      x: 0,
      y: i * 4,
      width: 100,
      height: 20,
    }));
    const first = separateLabels(stacked);
    expect(separateLabels(stacked)).toEqual(first);
    const tops = stacked.map((r, i) => r.y + first[i]).sort((a, b) => a - b);
    for (let i = 1; i < tops.length; i++)
      expect(tops[i] - tops[i - 1]).toBeGreaterThanOrEqual(26);
  });
});
