"use client";

import { useMemo, useRef, useState } from "react";
import type { GraphEdge, GraphNeighborhood, GraphNode } from "@/lib/api/types";
import styles from "./graph.module.css";
import { usePanZoom } from "./usePanZoom";

/**
 * Renders a backend graph neighbourhood exactly: the nodes and edges the
 * API returned, nothing inferred. Layout is a deterministic column layout:
 * the focus in the middle, symbols reached as dependents to the left (one
 * column per hop), dependencies to the right. Edges keep the stored
 * orientation, source = dependent -> target = dependency, so arrows flow
 * left to right through the focus.
 */

const COLUMN_GAP = 280;
const ROW_GAP = 34;
const NODE_RADIUS = 6;
const MIN_LABEL_ROOM = 180;
/** Approximate width of one character of the 12px monospace focus label. */
const FOCUS_CHAR_WIDTH = 7.5;

export interface PlacedNode extends GraphNode {
  x: number;
  y: number;
}

/** Signed column: dependents negative, dependencies positive, focus 0. */
function column(node: GraphNode): number {
  if (node.reached_via === "focus") return 0;
  if (node.reached_via === "dependencies") return node.depth;
  return -node.depth; // dependents, and nodes reached both ways
}

export function layoutNeighborhood(nodes: GraphNode[]): PlacedNode[] {
  const columns = new Map<number, GraphNode[]>();
  for (const node of nodes) {
    const key = column(node);
    columns.set(key, [...(columns.get(key) ?? []), node]);
  }
  const placed: PlacedNode[] = [];
  for (const [key, members] of columns) {
    const sorted = [...members].sort(
      (a, b) =>
        a.qualified_name.localeCompare(b.qualified_name) ||
        a.symbol_id.localeCompare(b.symbol_id),
    );
    sorted.forEach((node, index) => {
      placed.push({
        ...node,
        x: key * COLUMN_GAP,
        y: (index - (sorted.length - 1) / 2) * ROW_GAP,
      });
    });
  }
  return placed;
}

function shortLabel(name: string): string {
  return name.split(".").slice(-2).join(".");
}

function edgePath(source: PlacedNode, target: PlacedNode): string {
  if (source.x === target.x) {
    // Same column: bow out to the right so the edge stays visible.
    const bend = source.x + COLUMN_GAP * 0.35;
    return `M ${source.x + NODE_RADIUS} ${source.y} C ${bend} ${source.y}, ${bend} ${target.y}, ${target.x + NODE_RADIUS + 4} ${target.y}`;
  }
  const direction = target.x > source.x ? 1 : -1;
  const sx = source.x + direction * NODE_RADIUS;
  const tx = target.x - direction * (NODE_RADIUS + 4);
  const mid = (sx + tx) / 2;
  return `M ${sx} ${source.y} C ${mid} ${source.y}, ${mid} ${target.y}, ${tx} ${target.y}`;
}

export function NeighborhoodGraph({
  graph,
  selectedId,
  onSelect,
}: {
  graph: GraphNeighborhood;
  selectedId: string | null;
  onSelect: (node: GraphNode) => void;
}) {
  const placed = useMemo(() => layoutNeighborhood(graph.nodes), [graph.nodes]);
  const byId = useMemo(
    () => new Map(placed.map((n) => [n.symbol_id, n])),
    [placed],
  );
  const drawable = useMemo(
    () => graph.edges.filter((e) => byId.has(e.source) && byId.has(e.target)),
    [graph.edges, byId],
  );

  const xs = placed.map((n) => n.x);
  const ys = placed.map((n) => n.y);
  // Horizontal padding fits the focus node's full qualified name (other
  // nodes show a two-segment short label) without shrinking the drawing
  // more than needed.
  const focusName =
    placed.find((n) => n.reached_via === "focus")?.qualified_name ?? "";
  const labelRoom = Math.max(
    MIN_LABEL_ROOM,
    focusName.length * FOCUS_CHAR_WIDTH + 24,
  );
  const minX = Math.min(0, ...xs) - labelRoom;
  const maxX = Math.max(0, ...xs) + labelRoom;
  const minY = Math.min(0, ...ys) - 60;
  const maxY = Math.max(0, ...ys) + 60;
  const width = maxX - minX;

  const svgRef = useRef<SVGSVGElement>(null);
  const { transform, zoom, reset, handlers } = usePanZoom(svgRef, width);
  const [hoverId, setHoverId] = useState<string | null>(null);
  const activeId = hoverId ?? selectedId;
  const touches = (edge: GraphEdge) =>
    activeId !== null && (edge.source === activeId || edge.target === activeId);

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
        viewBox={`${minX} ${minY} ${width} ${maxY - minY}`}
        role="group"
        aria-label="Dependency graph neighbourhood. Arrow keys pan, plus and minus zoom, 0 resets. Tab to reach symbols."
        tabIndex={0}
        {...handlers}
      >
        <defs>
          <marker
            id="graph-arrow"
            viewBox="0 0 10 10"
            refX="9"
            refY="5"
            markerWidth="6"
            markerHeight="6"
            orient="auto-start-reverse"
          >
            <path d="M 0 0 L 10 5 L 0 10 z" className={styles.arrowHead} />
          </marker>
        </defs>
        <g transform={transform}>
          {drawable.map((edge) => {
            const source = byId.get(edge.source)!;
            const target = byId.get(edge.target)!;
            return (
              <path
                key={`${edge.source}-${edge.target}-${edge.relationship_type}`}
                d={edgePath(source, target)}
                className={`${styles.edge} ${styles[`rel-${edge.relationship_type}`] ?? ""} ${
                  touches(edge)
                    ? styles.edgeActive
                    : activeId
                      ? styles.edgeDim
                      : ""
                }`}
                markerEnd="url(#graph-arrow)"
                data-edge={`${edge.source}->${edge.target}`}
              >
                <title>
                  {`${source.qualified_name} ${edge.relationship_type} ${target.qualified_name}` +
                    (edge.occurrences > 1
                      ? ` (${edge.occurrences} sites)`
                      : "")}
                </title>
              </path>
            );
          })}

          {placed.map((node) => {
            const isFocus = node.reached_via === "focus";
            const active = activeId === node.symbol_id;
            return (
              <g
                key={node.symbol_id}
                data-node=""
                transform={`translate(${node.x} ${node.y})`}
                className={`${styles.node} ${isFocus ? styles.nodeFocus : ""} ${
                  selectedId === node.symbol_id ? styles.nodeSelected : ""
                } ${active ? styles.nodeActive : ""}`}
                role="button"
                tabIndex={0}
                aria-pressed={selectedId === node.symbol_id}
                aria-label={`${node.qualified_name}, ${node.kind}, ${
                  isFocus
                    ? "focus"
                    : `${node.reached_via} at depth ${node.depth}`
                }`}
                onClick={() => onSelect(node)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    onSelect(node);
                  }
                }}
                onPointerEnter={() => setHoverId(node.symbol_id)}
                onPointerLeave={() => setHoverId(null)}
                onFocus={() => setHoverId(node.symbol_id)}
                onBlur={() => setHoverId(null)}
              >
                <circle r={isFocus ? NODE_RADIUS + 2 : NODE_RADIUS} />
                <text
                  x={node.x < 0 ? -10 : 10}
                  y={4}
                  textAnchor={node.x < 0 ? "end" : "start"}
                  className={isFocus ? styles.targetLabel : styles.nodeLabel}
                >
                  {active || isFocus
                    ? node.qualified_name
                    : shortLabel(node.qualified_name)}
                </text>
              </g>
            );
          })}
        </g>
      </svg>
      <p className={styles.legend}>
        Left: dependents of the focus. Right: its dependencies. Arrows point
        from dependent to dependency (caller to callee, importer to imported,
        subclass to base). Every edge shown was returned by the graph API.
        {graph.truncated
          ? " The neighbourhood was truncated at the node or edge limit."
          : ""}
      </p>
    </div>
  );
}
