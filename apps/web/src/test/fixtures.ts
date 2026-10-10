/**
 * TEST-ONLY response fixtures. They exist to drive component tests through
 * the real API client (via a stubbed fetch); nothing in src/ outside
 * src/test and *.test.tsx files imports this module.
 */
import { vi } from "vitest";
import type {
  AnalysisStatusSummary,
  PipelineStatus,
  RepositoryOverview,
  StageAssessment,
} from "@/lib/api/types";

export const REPO_ID = "11111111-1111-4111-8111-111111111111";
export const REV_ID = "22222222-2222-4222-8222-222222222222";

const ANALYSIS_TYPES = [
  "ingestion",
  "parsing",
  "graph_build",
  "impact_analysis",
  "rag_indexing",
  "security_scan",
  "test_generation",
  "documentation_generation",
  "automation_analysis",
];

export function notRun(analysisType: string): AnalysisStatusSummary {
  return {
    analysis_type: analysisType,
    status: "not_run",
    analysis_run_id: null,
    analyzer_name: null,
    started_at: null,
    finished_at: null,
    error_message: null,
  };
}

/** A freshly ingested revision: ingestion completed, nothing else has run. */
export function ingestedOnlyOverview(): RepositoryOverview {
  const statuses = ANALYSIS_TYPES.map(notRun);
  statuses[0] = {
    ...statuses[0],
    status: "completed",
    analysis_run_id: "33333333-3333-4333-8333-333333333333",
    analyzer_name: "devlens-git-ingestion",
    started_at: "2026-10-01T10:00:00Z",
    finished_at: "2026-10-01T10:00:05Z",
  };
  return {
    repository: {
      repository_id: REPO_ID,
      owner: "acme",
      name: "billing",
      url: "https://github.com/acme/billing",
      default_branch: "main",
      status: "ready",
      current_revision_id: REV_ID,
    },
    revision: {
      revision_id: REV_ID,
      commit_sha: "abcdef0123456789abcdef0123456789abcdef01",
      parent_sha: null,
      branch_ref: null,
      committed_at: "2026-09-30T12:00:00Z",
      discovered_at: "2026-10-01T10:00:00Z",
      is_current: true,
      latest_analysis_status: "completed",
    },
    statistics: {
      stored_file_count: 42,
      recorded_file_count: 42,
      language_file_count: 30,
      total_source_bytes: 123456,
      files_by_language: [
        { key: "python", count: 30 },
        { key: "unknown", count: 12 },
      ],
      files_by_parsing_status: [{ key: "pending", count: 42 }],
      symbol_count: 0,
      symbols_by_kind: [],
    },
    structure: {
      root_file_count: 3,
      top_level_directory_count: 1,
      top_level_directories: [
        {
          name: "billing",
          file_count: 39,
          total_bytes: 120000,
          symbol_count: 0,
        },
      ],
      top_level_directories_truncated: false,
      python_module_count: 0,
      top_level_packages: [],
      top_level_packages_truncated: false,
    },
    graph: {
      edge_count: 0,
      internal_edge_count: 0,
      external_edge_count: 0,
      edges_by_relationship: [],
      node_count: 0,
      connected_node_count: 0,
    },
    important_components: {
      most_depended_upon_symbols: [],
      most_depended_upon_symbols_truncated: false,
      most_dependency_heavy_symbols: [],
      most_dependency_heavy_symbols_truncated: false,
      most_depended_upon_modules: [],
      most_depended_upon_modules_truncated: false,
      most_dependency_heavy_modules: [],
      most_dependency_heavy_modules_truncated: false,
    },
    analysis_statuses: statuses,
    security: {
      latest_run: notRun("security_scan"),
      findings_run_id: null,
      finding_count: 0,
      findings_by_severity: [],
      findings_by_current_triage_status: [],
    },
    testing: {
      latest_run: notRun("test_generation"),
      generated_test_count: 0,
      generated_tests_by_status: [],
      execution_count: 0,
      executions_by_status: [],
    },
    documentation: {
      latest_run: notRun("documentation_generation"),
      artifact_count: 0,
      artifacts_by_type: [],
      artifacts_by_generation_method: [],
      latest_artifact_updated_at: null,
    },
    rag: {
      chunking_run: notRun("rag_indexing"),
      embedding_run: notRun("rag_indexing"),
      chunk_count: 0,
      indexed_file_count: 0,
      embedding_count: 0,
    },
    automation: {
      latest_run: notRun("automation_analysis"),
      parent_revision_id: null,
      changed_file_count: null,
      changed_symbol_count: null,
      impacted_symbol_count: null,
      affected_file_count: null,
      limitation_count: null,
    },
    sessions: {
      session_count: 1,
      recent_sessions: [
        {
          session_id: "44444444-4444-4444-8444-444444444444",
          title: "Why do refunds double-post?",
          status: "active",
          last_active_at: "2026-10-02T09:30:00Z",
          message_count: 4,
        },
      ],
    },
    limitations: ["Test coverage is not reported."],
  };
}

export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** Stubs global fetch with a handler and returns the mock for call assertions. */
export function stubFetch(
  handler: (url: string, init?: RequestInit) => Response | Promise<Response>,
) {
  const mock = vi.fn((input: RequestInfo | URL, init?: RequestInit) =>
    Promise.resolve(handler(String(input), init)),
  );
  vi.stubGlobal("fetch", mock);
  return mock;
}

export function pipelineStatus(
  overrides: Partial<Record<string, Partial<StageAssessment>>> = {},
): PipelineStatus {
  const stage = (name: StageAssessment["stage"]): StageAssessment => ({
    stage: name,
    status: "not_run",
    up_to_date: false,
    analysis_run_id: null,
    started_at: null,
    finished_at: null,
    detail: null,
    ...overrides[name],
  });
  return {
    repository_id: REPO_ID,
    revision_id: REV_ID,
    stages: [
      stage("parsing"),
      stage("graph"),
      stage("rag_chunking"),
      {
        ...stage("rag_embedding"),
        status: "unavailable",
        detail:
          "no embedding provider is configured; lexical retrieval still works",
        ...overrides.rag_embedding,
      },
    ],
    analysis_in_progress: false,
  };
}

type Route = { method?: string; match: string; respond: () => Response };

/**
 * Stubs fetch with an ordered route table (first match on substring and
 * method wins). Unmatched requests fail the test loudly.
 */
export function routeFetch(routes: Route[]) {
  return stubFetch((url, init) => {
    const method = init?.method ?? "GET";
    const route = routes.find(
      (r) => url.includes(r.match) && (r.method ?? "GET") === method,
    );
    if (!route) throw new Error(`unexpected request: ${method} ${url}`);
    return route.respond();
  });
}
