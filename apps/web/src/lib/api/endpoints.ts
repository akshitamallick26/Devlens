import { expectShape, request } from "./client";
import type {
  AnalyzeRequest,
  AutomationResponse,
  ContextRef,
  ContextRefRequest,
  DirectoryListing,
  FileDetail,
  FindingPage,
  GeneratedTestPage,
  GraphDirection,
  GraphNeighborhood,
  PipelineResult,
  PipelineStatus,
  RevisionPage,
  SourceRange,
  SymbolSearchResult,
  DocumentationArtifact,
  DocumentationListResponse,
  GenerateDocumentationRequest,
  GenerateTestsRequest,
  GenerateTestsResponse,
  ImpactRequest,
  ImpactResponse,
  IngestResponse,
  QAResponse,
  RecentSessionsResponse,
  RepositoryOverview,
  SecurityAnalysisRequest,
  SecurityAnalysisResponse,
  SessionDetail,
} from "./types";

/**
 * One typed function per backend route the workstation uses. Paths and
 * request bodies match apps/api/app/api/routes/*.py exactly. Every function
 * accepts an AbortSignal so views can cancel on unmount/navigation.
 */

const repo = (repositoryId: string) =>
  `/repositories/${encodeURIComponent(repositoryId)}`;

export function getOverview(
  repositoryId: string,
  revisionId: string | null,
  signal?: AbortSignal,
): Promise<RepositoryOverview> {
  return request(
    `${repo(repositoryId)}/overview`,
    (body) => {
      const overview = expectShape<RepositoryOverview>(
        body,
        [
          "repository",
          "revision",
          "statistics",
          "structure",
          "graph",
          "important_components",
          "security",
          "testing",
          "documentation",
          "rag",
          "automation",
          "sessions",
        ],
        ["analysis_statuses", "limitations"],
      );
      expectShape(overview.repository, ["repository_id", "owner", "name"]);
      expectShape(overview.revision, [
        "revision_id",
        "commit_sha",
        "is_current",
      ]);
      expectShape(
        overview.statistics,
        ["stored_file_count", "symbol_count"],
        ["files_by_language", "symbols_by_kind"],
      );
      expectShape(
        overview.structure,
        ["root_file_count"],
        ["top_level_directories", "top_level_packages"],
      );
      expectShape(overview.graph, ["edge_count"], ["edges_by_relationship"]);
      expectShape(
        overview.important_components,
        [],
        [
          "most_depended_upon_symbols",
          "most_dependency_heavy_symbols",
          "most_depended_upon_modules",
          "most_dependency_heavy_modules",
        ],
      );
      expectShape(overview.sessions, ["session_count"], ["recent_sessions"]);
      return overview;
    },
    { query: { revision_id: revisionId }, signal },
  );
}

export function ingestRepository(
  url: string,
  signal?: AbortSignal,
): Promise<IngestResponse> {
  return request(
    "/repositories/ingest",
    (body) =>
      expectShape<IngestResponse>(body, [
        "repository_id",
        "revision_id",
        "commit_sha",
      ]),
    { method: "POST", body: { url }, signal },
  );
}

export function analyzeImpact(
  repositoryId: string,
  payload: ImpactRequest,
  signal?: AbortSignal,
): Promise<ImpactResponse> {
  return request(
    `${repo(repositoryId)}/impact`,
    (body) =>
      expectShape<ImpactResponse>(
        body,
        [
          "target_symbol_id",
          "target_qualified_name",
          "max_depth",
          "analysis_status",
        ],
        ["direct_impacts", "transitive_impacts", "impacted_files"],
      ),
    { method: "POST", body: payload, signal },
  );
}

export function askQuestion(
  repositoryId: string,
  payload: { revision_id: string; question: string; top_k?: number },
  signal?: AbortSignal,
): Promise<QAResponse> {
  return request(
    `${repo(repositoryId)}/qa`,
    (body) =>
      expectShape<QAResponse>(
        body,
        ["analysis_status", "question"],
        ["evidence_items"],
      ),
    { method: "POST", body: payload, signal },
  );
}

export function runSecurityAnalysis(
  repositoryId: string,
  payload: SecurityAnalysisRequest,
  signal?: AbortSignal,
): Promise<SecurityAnalysisResponse> {
  return request(
    `${repo(repositoryId)}/security/analyze`,
    (body) =>
      expectShape<SecurityAnalysisResponse>(
        body,
        ["analysis_status"],
        ["scanner_results", "findings", "limitations"],
      ),
    { method: "POST", body: payload, signal },
  );
}

export function generateTests(
  repositoryId: string,
  payload: GenerateTestsRequest,
  signal?: AbortSignal,
): Promise<GenerateTestsResponse> {
  return request(
    `${repo(repositoryId)}/testing/generate`,
    (body) =>
      expectShape<GenerateTestsResponse>(
        body,
        ["analysis_status", "limitations"],
        ["outcomes"],
      ),
    { method: "POST", body: payload, signal },
  );
}

export function runAutomationAnalysis(
  repositoryId: string,
  payload: { revision_id: string; parent_revision_id?: string },
  signal?: AbortSignal,
): Promise<AutomationResponse> {
  return request(
    `${repo(repositoryId)}/automation/analyze`,
    (body) =>
      expectShape<AutomationResponse>(
        body,
        [
          "analysis_status",
          "file_delta",
          "symbol_delta",
          "parsing_execution",
          "graph_execution",
          "rag_invalidation",
          "testing_invalidation",
          "security_invalidation",
          "summary",
        ],
        ["limitations"],
      ),
    { method: "POST", body: payload, signal },
  );
}

export function listDocumentation(
  repositoryId: string,
  revisionId: string,
  signal?: AbortSignal,
): Promise<DocumentationListResponse> {
  return request(
    `${repo(repositoryId)}/documentation`,
    (body) => expectShape<DocumentationListResponse>(body, [], ["artifacts"]),
    { query: { revision_id: revisionId }, signal },
  );
}

export function getDocumentation(
  repositoryId: string,
  artifactId: string,
  signal?: AbortSignal,
): Promise<DocumentationArtifact> {
  return request(
    `${repo(repositoryId)}/documentation/${encodeURIComponent(artifactId)}`,
    (body) =>
      expectShape<DocumentationArtifact>(
        body,
        ["artifact_id", "content", "generation_status", "context_truncated"],
        ["evidence_ids", "cited_evidence_ids"],
      ),
    { signal },
  );
}

export function generateDocumentation(
  repositoryId: string,
  payload: GenerateDocumentationRequest,
  signal?: AbortSignal,
): Promise<DocumentationArtifact> {
  return request(
    `${repo(repositoryId)}/documentation/generate`,
    (body) =>
      expectShape<DocumentationArtifact>(
        body,
        ["artifact_id", "content", "generation_status"],
        ["evidence_ids", "cited_evidence_ids"],
      ),
    { method: "POST", body: payload, signal },
  );
}

export function listSessions(
  repositoryId: string,
  options: { limit?: number; offset?: number } = {},
  signal?: AbortSignal,
): Promise<RecentSessionsResponse> {
  return request(
    `${repo(repositoryId)}/sessions`,
    (body) =>
      expectShape<RecentSessionsResponse>(body, ["total_count"], ["sessions"]),
    { query: { limit: options.limit, offset: options.offset }, signal },
  );
}

/** Read-only on the backend: never updates last_active_at. */
export function getSession(
  repositoryId: string,
  sessionId: string,
  options: { messageLimit?: number; beforeSequence?: number | null } = {},
  signal?: AbortSignal,
): Promise<SessionDetail> {
  return request(
    `${repo(repositoryId)}/sessions/${encodeURIComponent(sessionId)}`,
    (body) => {
      const detail = expectShape<SessionDetail>(
        body,
        ["id", "messages", "status"],
        ["context_refs"],
      );
      expectShape(detail.messages, ["has_more"], ["messages"]);
      return detail;
    },
    {
      query: {
        message_limit: options.messageLimit,
        message_before_sequence: options.beforeSequence ?? undefined,
      },
      signal,
    },
  );
}

// ---------- Analysis pipeline ----------

const rev = (repositoryId: string, revisionId: string) =>
  `${repo(repositoryId)}/revisions/${encodeURIComponent(revisionId)}`;

/** Synchronous on the backend: resolves when every requested stage has finished. */
export function analyzeRevision(
  repositoryId: string,
  revisionId: string,
  payload: AnalyzeRequest = {},
  signal?: AbortSignal,
): Promise<PipelineResult> {
  return request(
    `${rev(repositoryId, revisionId)}/analyze`,
    (body) =>
      expectShape<PipelineResult>(
        body,
        ["overall_status", "execution"],
        ["stages"],
      ),
    { method: "POST", body: payload, signal },
  );
}

/** Read-only: creates no analysis run. */
export function getPipelineStatus(
  repositoryId: string,
  revisionId: string,
  signal?: AbortSignal,
): Promise<PipelineStatus> {
  return request(
    `${rev(repositoryId, revisionId)}/analysis`,
    (body) =>
      expectShape<PipelineStatus>(body, ["analysis_in_progress"], ["stages"]),
    { signal },
  );
}

// ---------- Code intelligence reads ----------

export function listRevisions(
  repositoryId: string,
  options: { limit?: number; offset?: number } = {},
  signal?: AbortSignal,
): Promise<RevisionPage> {
  return request(
    `${repo(repositoryId)}/revisions`,
    (body) => expectShape<RevisionPage>(body, ["total_count"], ["revisions"]),
    { query: { limit: options.limit, offset: options.offset }, signal },
  );
}

export function browseFiles(
  repositoryId: string,
  revisionId: string,
  options: { directory?: string; limit?: number; offset?: number } = {},
  signal?: AbortSignal,
): Promise<DirectoryListing> {
  return request(
    `${rev(repositoryId, revisionId)}/files`,
    (body) =>
      expectShape<DirectoryListing>(
        body,
        ["directory", "total_files"],
        ["directories", "files"],
      ),
    {
      query: {
        directory: options.directory,
        limit: options.limit,
        offset: options.offset,
      },
      signal,
    },
  );
}

export function getFileDetail(
  repositoryId: string,
  revisionId: string,
  fileId: string,
  signal?: AbortSignal,
): Promise<FileDetail> {
  return request(
    `${rev(repositoryId, revisionId)}/files/${encodeURIComponent(fileId)}`,
    (body) => {
      const detail = expectShape<FileDetail>(
        body,
        ["file", "source_available"],
        ["symbols"],
      );
      expectShape(detail.file, ["file_id", "path"]);
      return detail;
    },
    { signal },
  );
}

export function getSource(
  repositoryId: string,
  revisionId: string,
  fileId: string,
  range: { startLine: number; endLine?: number },
  signal?: AbortSignal,
): Promise<SourceRange> {
  return request(
    `${rev(repositoryId, revisionId)}/files/${encodeURIComponent(fileId)}/source`,
    (body) =>
      expectShape<SourceRange>(
        body,
        ["start_line", "end_line", "total_lines", "truncated"],
        ["lines"],
      ),
    { query: { start_line: range.startLine, end_line: range.endLine }, signal },
  );
}

export function searchSymbols(
  repositoryId: string,
  revisionId: string,
  query: string,
  signal?: AbortSignal,
): Promise<SymbolSearchResult> {
  return request(
    `${rev(repositoryId, revisionId)}/symbols`,
    (body) => expectShape<SymbolSearchResult>(body, ["truncated"], ["symbols"]),
    { query: { q: query, limit: 20 }, signal },
  );
}

export function getGraphNeighborhood(
  repositoryId: string,
  revisionId: string,
  params: {
    symbolId: string;
    direction: GraphDirection;
    depth: number;
    limit: number;
  },
  signal?: AbortSignal,
): Promise<GraphNeighborhood> {
  return request(
    `${rev(repositoryId, revisionId)}/graph/neighborhood`,
    (body) =>
      expectShape<GraphNeighborhood>(
        body,
        ["focus_symbol_id", "truncated"],
        ["nodes", "edges", "focus_external_references"],
      ),
    {
      query: {
        symbol_id: params.symbolId,
        direction: params.direction,
        depth: params.depth,
        limit: params.limit,
      },
      signal,
    },
  );
}

export function listPersistedFindings(
  repositoryId: string,
  revisionId: string,
  options: { limit?: number; offset?: number } = {},
  signal?: AbortSignal,
): Promise<FindingPage> {
  return request(
    `${rev(repositoryId, revisionId)}/security/findings`,
    (body) => expectShape<FindingPage>(body, ["total_count"], ["findings"]),
    { query: { limit: options.limit, offset: options.offset }, signal },
  );
}

export function listPersistedTests(
  repositoryId: string,
  revisionId: string,
  options: { limit?: number; offset?: number } = {},
  signal?: AbortSignal,
): Promise<GeneratedTestPage> {
  return request(
    `${rev(repositoryId, revisionId)}/testing/generated-tests`,
    (body) => expectShape<GeneratedTestPage>(body, ["total_count"], ["tests"]),
    { query: { limit: options.limit, offset: options.offset }, signal },
  );
}

// ---------- Session writes (explicit user actions only) ----------

export function createSession(
  repositoryId: string,
  payload: { revision_id: string; title: string },
  signal?: AbortSignal,
): Promise<SessionDetail> {
  return request(
    `${repo(repositoryId)}/sessions`,
    (body) => expectShape<SessionDetail>(body, ["id", "status"]),
    { method: "POST", body: payload, signal },
  );
}

export function addContextRef(
  repositoryId: string,
  sessionId: string,
  payload: ContextRefRequest,
  signal?: AbortSignal,
): Promise<ContextRef> {
  return request(
    `${repo(repositoryId)}/sessions/${encodeURIComponent(sessionId)}/context`,
    (body) => expectShape<ContextRef>(body, ["id", "ref_type"]),
    { method: "POST", body: payload, signal },
  );
}
