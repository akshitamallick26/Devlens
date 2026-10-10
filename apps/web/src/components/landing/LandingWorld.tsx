"use client";

import { useEffect, useRef } from "react";
import styles from "./landing.module.css";

type Mode = "pending" | "webgl" | "static" | "none";

interface SceneTracker {
  progress: () => number;
  dispose: () => void;
}

/**
 * Maps scroll position to one continuous scene position: the index of the
 * [data-scene] section under the viewport's center plus the fraction of it
 * scrolled. Section offsets are cached and re-measured on resize, so a
 * scroll event costs one loop over ten cached numbers.
 */
function trackScenes(onScene: (index: number) => void): SceneTracker {
  const sections = Array.from(
    document.querySelectorAll<HTMLElement>("[data-scene]"),
  );
  const tops = new Float64Array(sections.length);
  const heights = new Float64Array(sections.length);
  let progress = 0;
  let scene = -1;

  const update = () => {
    const center = window.scrollY + window.innerHeight * 0.5;
    let next = 0;
    for (let i = 0; i < sections.length; i++) {
      if (center >= tops[i]) {
        next = i + Math.min(1, (center - tops[i]) / Math.max(heights[i], 1));
      }
    }
    progress = next;
    const index = Math.min(Math.floor(next), sections.length - 1);
    if (index !== scene) {
      scene = index;
      onScene(index);
    }
  };
  const measure = () => {
    const scrollY = window.scrollY;
    sections.forEach((section, i) => {
      const rect = section.getBoundingClientRect();
      tops[i] = rect.top + scrollY;
      heights[i] = rect.height;
    });
    update();
  };

  const observer =
    typeof ResizeObserver === "undefined" ? null : new ResizeObserver(measure);
  observer?.observe(document.body);
  window.addEventListener("scroll", update, { passive: true });
  window.addEventListener("resize", measure);
  measure();
  return {
    progress: () => progress,
    dispose: () => {
      observer?.disconnect();
      window.removeEventListener("scroll", update);
      window.removeEventListener("resize", measure);
    },
  };
}

function matches(query: string): boolean {
  return (
    typeof window.matchMedia === "function" && window.matchMedia(query).matches
  );
}

function supportsWebGL2(): boolean {
  try {
    const probe = document.createElement("canvas");
    const context = probe.getContext("webgl2");
    context?.getExtension("WEBGL_lose_context")?.loseContext();
    return context !== null;
  } catch {
    return false;
  }
}

function whenIdle(callback: () => void): () => void {
  if (typeof window.requestIdleCallback === "function") {
    const handle = window.requestIdleCallback(callback, { timeout: 700 });
    return () => window.cancelIdleCallback(handle);
  }
  const handle = window.setTimeout(callback, 120);
  return () => window.clearTimeout(handle);
}

/**
 * The decorative world behind the landing narrative. Everything it shows is
 * also stated in the page's text, so it is hidden from assistive technology.
 * It never calls the API: its data is a static export bundled with it.
 */
export function LandingWorld() {
  const hostRef = useRef<HTMLDivElement>(null);
  const tooltipRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const host = hostRef.current;
    const tooltip = tooltipRef.current;
    if (!host || !tooltip) return;
    let disposed = false;
    let teardown: (() => void) | null = null;
    let drawStatic: ((scene: number) => void) | null = null;
    let currentScene = 0;

    const setMode = (mode: Mode) => host.setAttribute("data-mode", mode);
    const tracker = trackScenes((scene) => {
      currentScene = scene;
      drawStatic?.(scene);
    });

    const reducedMotion = matches("(prefers-reduced-motion: reduce)");
    const lite = matches("(max-width: 720px), (pointer: coarse)");

    const mountCanvas = () => {
      const canvas = document.createElement("canvas");
      canvas.className = styles.canvas;
      canvas.setAttribute("aria-hidden", "true");
      host.prepend(canvas);
      return canvas;
    };

    const startStatic = async () => {
      teardown?.();
      teardown = null;
      const [{ graphData }, { createStaticGraph }] = await Promise.all([
        import("./data/graph"),
        import("./staticGraph"),
      ]);
      if (disposed) return;
      const canvas = mountCanvas();
      const graph = createStaticGraph(canvas, graphData);
      drawStatic = graph.draw;
      graph.draw(currentScene);
      setMode("static");
      teardown = () => {
        drawStatic = null;
        graph.dispose();
        canvas.remove();
      };
    };

    const hideTooltip = () => {
      tooltip.hidden = true;
    };
    const moveTooltip = (x: number, y: number) => {
      tooltip.style.transform = `translate(${x + 14}px, ${y + 14}px)`;
    };

    const startWebGL = async () => {
      const [{ graphData }, { createWorld }] = await Promise.all([
        import("./data/graph"),
        import("./world/createWorld"),
      ]);
      const mono =
        getComputedStyle(document.documentElement)
          .getPropertyValue("--font-geist-mono")
          .trim() || "monospace";
      await document.fonts?.ready;
      if (disposed) return;
      const canvas = mountCanvas();
      try {
        const world = createWorld(canvas, graphData, {
          lite,
          fontFamily: mono,
          getProgress: tracker.progress,
          onHover: (info, x, y) => {
            if (!info) return hideTooltip();
            tooltip.textContent = "";
            const name = document.createElement("span");
            name.textContent = info.name;
            const meta = document.createElement("span");
            meta.textContent = `${info.kind} in ${info.packageName}`;
            tooltip.append(name, meta);
            tooltip.hidden = false;
            moveTooltip(x, y);
          },
          onHoverMove: moveTooltip,
          onContextLost: () => void startStatic(),
        });
        setMode("webgl");
        teardown = () => {
          world.dispose();
          canvas.remove();
          hideTooltip();
        };
      } catch {
        canvas.remove();
        await startStatic();
      }
    };

    setMode("pending");
    const cancelIdle = whenIdle(() => {
      if (disposed) return;
      const run =
        !reducedMotion && supportsWebGL2() ? startWebGL() : startStatic();
      run.catch(() => setMode("none"));
    });

    return () => {
      disposed = true;
      cancelIdle();
      tracker.dispose();
      teardown?.();
      teardown = null;
    };
  }, []);

  return (
    <div
      ref={hostRef}
      className={styles.world}
      aria-hidden="true"
      data-mode="pending"
    >
      <div ref={tooltipRef} className={styles.tooltip} hidden />
    </div>
  );
}
