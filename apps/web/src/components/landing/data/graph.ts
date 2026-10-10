/**
 * Real DevLens output for its own backend (apps/api/app), exported once by
 * running ingestion, parsing, graph construction and the graph/neighborhood
 * dependents traversal against a snapshot of that source. Symbols, edges and
 * the impact set are product output; only `positions` is an offline 3D
 * layout (the product has no layout of its own). See `provenance`.
 */
import raw from "./devlensGraph.json";

export type GraphData = typeof raw;

export const graphData: GraphData = raw;

/** Impact depth per node index; -1 when the node is not in the impacted set. */
export function impactDepths(data: GraphData): Int8Array {
  const depths = new Int8Array(data.names.length).fill(-1);
  for (const [node, depth] of Object.entries(data.impact.depthByNode)) {
    depths[Number(node)] = depth;
  }
  return depths;
}

/** The node index of a module symbol, by repository-relative file path. */
export function moduleNodeForFile(data: GraphData, file: string): number {
  const moduleName = file
    .replace(/^apps\/api\//, "")
    .replace(/\.py$/, "")
    .replace(/\/__init__$/, "")
    .split("/")
    .join(".");
  return data.names.indexOf(moduleName);
}
