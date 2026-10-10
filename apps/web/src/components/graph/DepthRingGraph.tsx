"use client";

import { useMemo, useRef, useState } from "react";
import { usePanZoom } from "./usePanZoom";
import styles from "./graph.module.css";

/**
 * Dependents of one target, placed on concentric rings by shortest
 * dependency distance (ring 1 = direct dependents). The impact API returns
 * each dependent's depth and relationship types but not the edges between
 * dependents, so this view draws spokes only from depth-1 dependents to the
 * target (those edges are real: each one depends directly on the target)
 * and draws no edges between rings. Arrows point dependent -> dependency.
 *
 * Layout is a deterministic function of the input order; no physics, no
 * graph analytics in the browser.
 */

export interface RingNode {
  id: string;
  label: string;
  depth: number;
  detail?: string;
}

export const MAX_RENDERED_NODES = 150;
const RING_GAP = 110;
const NODE_RADIUS = 6;

interface Placed extends RingNode {
  x: number;
  y: number;
}

export function layoutRings(nodes: RingNode[]): {
  placed: Placed[];
  maxDepth: number;
} {
  const byDepth = new Map<number, RingNode[]>();
  for (const node of nodes) {
    const ring = byDepth.get(node.depth) ?? [];
    ring.push(node);
    byDepth.set(node.depth, ring);
  }
  const placed: Placed[] = [];
  let maxDepth = 0;
  for (const [depth, ring] of [...byDepth.entries()].sort(
    (a, b) => a[0] - b[0],
  )) {
    maxDepth = Math.max(maxDepth, depth);
    const radius = depth * RING_GAP;
    ring.forEach((node, index) => {
      const angle =
        (2 * Math.PI * index) / ring.length - Math.PI / 2 + depth * 0.35;
      placed.push({
        ...node,
        x: Math.cos(angle) * radius,
        y: Math.sin(angle) * radius,
      });
    });
  }
  return { placed, maxDepth };
}

function shortLabel(label: string): string {
  const parts = label.split(".");
  return parts.slice(-2).join(".");
}

export function DepthRingGraph({
  target,
  nodes,
  selectedId,
  onSelect,
  label,
}: {
  target: { id: string; label: string };
  nodes: RingNode[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  label: string;
}) {
  const rendered = useMemo(() => nodes.slice(0, MAX_RENDERED_NODES), [nodes]);
  const { placed, maxDepth } = useMemo(() => layoutRings(rendered), [rendered]);
  const extent = Math.max(1, maxDepth) * RING_GAP + 80;

  const [hoverId, setHoverId] = useState<string | null>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const { transform, zoom, reset, handlers } = usePanZoom(svgRef, extent * 2);

  const focusId = hoverId ?? selectedId;

  return (
    <div className={styles.wrap}>
      <div className={styles.controls}>
        <button type="button" onClick={() => zoom(1.2)} aria-label="Zoom in">
          +
        </button>
        <button
          type="button"
          onClick={() => zoom(1 / 1.2)}
          aria-label="Zoom out"
        >
          -
        </button>
        <button type="button" onClick={reset}>
          Reset view
        </button>
      </div>
      <svg
        ref={svgRef}
        className={styles.canvas}
        viewBox={`${-extent} ${-extent} ${extent * 2} ${extent * 2}`}
        role="group"
        aria-label={`${label}. Arrow keys pan, plus and minus zoom, 0 resets. Tab to reach symbols.`}
        tabIndex={0}
        {...handlers}
      >
        <defs>
          <marker
            id="arrow"
            viewBox="0 0 10 10"
            refX="9"
            refY="5"
            markerWidth="5"
            markerHeight="5"
            orient="auto-start-reverse"
          >
            <path d="M 0 0 L 10 5 L 0 10 z" className={styles.arrowHead} />
          </marker>
        </defs>
        <g transform={transform}>
          {Array.from({ length: maxDepth }, (_, i) => (
            <g key={i}>
              <circle r={(i + 1) * RING_GAP} className={styles.ring} />
              <text
                x={4}
                y={-(i + 1) * RING_GAP - 4}
                className={styles.ringLabel}
              >
                depth {i + 1}
              </text>
            </g>
          ))}

          {placed
            .filter((node) => node.depth === 1)
            .map((node) => {
              const length = Math.hypot(node.x, node.y) || 1;
              const inset = (NODE_RADIUS + 8) / length;
              return (
                <line
                  key={`edge-${node.id}`}
                  x1={node.x * (1 - inset)}
                  y1={node.y * (1 - inset)}
                  x2={node.x * inset}
                  y2={node.y * inset}
                  className={`${styles.edge} ${focusId === node.id ? styles.edgeActive : ""}`}
                  markerEnd="url(#arrow)"
                />
              );
            })}

          <g className={styles.target}>
            <circle r={NODE_RADIUS + 3} />
            <text y={-14} textAnchor="middle" className={styles.targetLabel}>
              {shortLabel(target.label)}
            </text>
          </g>

          {placed.map((node) => {
            const active = focusId === node.id;
            return (
              <g
                key={node.id}
                data-node=""
                transform={`translate(${node.x} ${node.y})`}
                className={`${styles.node} ${selectedId === node.id ? styles.nodeSelected : ""} ${active ? styles.nodeActive : ""}`}
                role="button"
                tabIndex={0}
                aria-pressed={selectedId === node.id}
                aria-label={`${node.label}, depth ${node.depth}${node.detail ? `, ${node.detail}` : ""}`}
                onClick={() => onSelect(node.id)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    onSelect(node.id);
                  }
                }}
                onPointerEnter={() => setHoverId(node.id)}
                onPointerLeave={() => setHoverId(null)}
                onFocus={() => setHoverId(node.id)}
                onBlur={() => setHoverId(null)}
              >
                <circle r={NODE_RADIUS} />
                {active || placed.length <= 40 ? (
                  <text
                    y={-10}
                    textAnchor="middle"
                    className={styles.nodeLabel}
                  >
                    {active ? node.label : shortLabel(node.label)}
                  </text>
                ) : null}
              </g>
            );
          })}
        </g>
      </svg>
      <p className={styles.legend}>
        Center: target. Rings: shortest dependency distance from it. Arrows:
        dependent to dependency (shown for direct dependents only; the API does
        not return edges between transitive dependents).
        {nodes.length > MAX_RENDERED_NODES
          ? ` Showing the first ${MAX_RENDERED_NODES} of ${nodes.length} dependents; the table lists all.`
          : ""}
      </p>
    </div>
  );
}
