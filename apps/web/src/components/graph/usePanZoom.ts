"use client";

import {
  type KeyboardEvent,
  type PointerEvent,
  type RefObject,
  useEffect,
  useRef,
  useState,
} from "react";

/**
 * Pan/zoom state for an SVG canvas whose viewBox spans `unitsWide` user
 * units. Mouse: drag the background to pan, wheel to zoom. Keyboard (canvas
 * focused): arrows pan, +/- zoom, 0 resets. Nodes opt out of drag panning
 * with a `data-node` attribute.
 */
export function usePanZoom(
  svgRef: RefObject<SVGSVGElement | null>,
  unitsWide: number,
) {
  const [view, setView] = useState({ x: 0, y: 0, scale: 1 });
  const drag = useRef<{
    x: number;
    y: number;
    originX: number;
    originY: number;
  } | null>(null);

  const zoom = (factor: number) =>
    setView((v) => ({
      ...v,
      scale: Math.min(4, Math.max(0.2, v.scale * factor)),
    }));
  const reset = () => setView({ x: 0, y: 0, scale: 1 });

  // Native non-passive listener: React's onWheel is passive and could not
  // stop the page from scrolling while zooming.
  useEffect(() => {
    const svg = svgRef.current;
    if (!svg) return;
    function onWheel(event: globalThis.WheelEvent) {
      event.preventDefault();
      const factor = event.deltaY < 0 ? 1.15 : 1 / 1.15;
      setView((v) => ({
        ...v,
        scale: Math.min(4, Math.max(0.2, v.scale * factor)),
      }));
    }
    svg.addEventListener("wheel", onWheel, { passive: false });
    return () => svg.removeEventListener("wheel", onWheel);
  }, [svgRef]);

  const handlers = {
    onPointerDown(event: PointerEvent<SVGSVGElement>) {
      if ((event.target as Element).closest("[data-node]")) return;
      drag.current = {
        x: event.clientX,
        y: event.clientY,
        originX: view.x,
        originY: view.y,
      };
      svgRef.current?.setPointerCapture?.(event.pointerId);
    },
    onPointerMove(event: PointerEvent<SVGSVGElement>) {
      if (!drag.current || !svgRef.current) return;
      const rect = svgRef.current.getBoundingClientRect();
      const unitsPerPixel = unitsWide / Math.max(rect.width, 1) / view.scale;
      const start = drag.current;
      setView((v) => ({
        ...v,
        x: start.originX + (event.clientX - start.x) * unitsPerPixel,
        y: start.originY + (event.clientY - start.y) * unitsPerPixel,
      }));
    },
    onPointerUp() {
      drag.current = null;
    },
    onPointerCancel() {
      drag.current = null;
    },
    onKeyDown(event: KeyboardEvent<SVGSVGElement>) {
      if (event.target !== event.currentTarget) return;
      const step = 40 / view.scale;
      const moves: Record<string, [number, number]> = {
        ArrowLeft: [step, 0],
        ArrowRight: [-step, 0],
        ArrowUp: [0, step],
        ArrowDown: [0, -step],
      };
      if (moves[event.key]) {
        event.preventDefault();
        const [dx, dy] = moves[event.key];
        setView((v) => ({ ...v, x: v.x + dx, y: v.y + dy }));
      } else if (event.key === "+" || event.key === "=") zoom(1.2);
      else if (event.key === "-") zoom(1 / 1.2);
      else if (event.key === "0") reset();
    },
  };

  return {
    transform: `translate(${view.x} ${view.y}) scale(${view.scale})`,
    zoom,
    reset,
    handlers,
  };
}
