/**
 * Typed mirrors of the DevLens API response shapes. Each interface matches a
 * backend Pydantic/dataclass response field for field (UUIDs and datetimes
 * arrive as strings). These are the only domain types the frontend uses;
 * components never redefine them.
 *
 * Source of truth: apps/api/app/api/routes/*.py and
 * apps/api/app/overview/models.py.
 */

// ---------- Shared ----------

export interface CountByKey {
  key: string;
  count: number;
}

export interface EvidenceLocation {
  file_path: string;
  line_start: number | null;
  line_end: number | null;
}

// ---------- Repository Overview (GET /repositories/{id}/overview) ----------

/** `not_run` is the only value the overview adds to AnalysisRun.status. */
export type AnalysisRunStatus =
  | "not_run"
  | "pending"
  | "running"
  | "completed"
  | "partial"
  | "failed"
  | "cancelled";

export interface RepositorySummary {
  repository_id: string;
  owner: string;
  name: string;
  url: string;
  default_branch: string;
  status: string;
  current_revision_id: string | null;
}

export interface RevisionSummary {
  revision_id: string;
  commit_sha: string;
  parent_sha: string | null;
  branch_ref: string | null;
  committed_at: string;
  discovered_at: string;
  is_current: boolean;
  latest_analysis_status: string | null;
}

export interface RepositoryStatistics {
  stored_file_count: number;
  recorded_file_count: number | null;
  language_file_count: number;
  total_source_bytes: number;
  files_by_language: CountByKey[];
  files_by_parsing_status: CountByKey[];
  symbol_count: number;
  symbols_by_kind: CountByKey[];
}

export interface DirectoryEntry {
  name: string;
  file_count: number;
  total_bytes: number;
  symbol_count: number;
}

export interface PackageEntry {
  qualified_name: string;
  file_path: string;
}

export interface RepositoryStructure {
  root_file_count: number;
  top_level_directory_count: number;
  top_level_directories: DirectoryEntry[];
  top_level_directories_truncated: boolean;
  python_module_count: number;
  top_level_packages: PackageEntry[];
  top_level_packages_truncated: boolean;
}

export interface GraphSummary {
  edge_count: number;
  internal_edge_count: number;
  external_edge_count: number;
  edges_by_relationship: CountByKey[];
  node_count: number;
  connected_node_count: number;
}

/** in_degree = direct dependents; out_degree = direct internal dependencies. */
export interface ImportantComponent {
  symbol_id: string;
  qualified_name: string;
  kind: string;
  file_path: string;
  in_degree: number;
  out_degree: number;
}

export interface ModuleComponent {
  file_id: string;
  file_path: string;
  module_name: string | null;
  in_degree: number;
  out_degree: number;
}

export interface ImportantComponents {
  most_depended_upon_symbols: ImportantComponent[];
  most_depended_upon_symbols_truncated: boolean;
  most_dependency_heavy_symbols: ImportantComponent[];
  most_dependency_heavy_symbols_truncated: boolean;
  most_depended_upon_modules: ModuleComponent[];
  most_depended_upon_modules_truncated: boolean;
  most_dependency_heavy_modules: ModuleComponent[];
  most_dependency_heavy_modules_truncated: boolean;
}

export interface AnalysisStatusSummary {
  analysis_type: string;
  status: AnalysisRunStatus;
  analysis_run_id: string | null;
  analyzer_name: string | null;
  started_at: string | null;
  finished_at: string | null;
  error_message: string | null;
}

export interface SecuritySummary {
  latest_run: AnalysisStatusSummary;
  findings_run_id: string | null;
  finding_count: number;
  findings_by_severity: CountByKey[];
  findings_by_current_triage_status: CountByKey[];
}

export interface GeneratedTestSummary {
  latest_run: AnalysisStatusSummary;
  generated_test_count: number;
  generated_tests_by_status: CountByKey[];
  execution_count: number;
  executions_by_status: CountByKey[];
}

export interface DocumentationSummary {
  latest_run: AnalysisStatusSummary;
  artifact_count: number;
  artifacts_by_type: CountByKey[];
  artifacts_by_generation_method: CountByKey[];
  latest_artifact_updated_at: string | null;
}

export interface RagSummary {
  chunking_run: AnalysisStatusSummary;
  embedding_run: AnalysisStatusSummary;
  chunk_count: number;
  indexed_file_count: number;
  embedding_count: number;
}

/** Count fields are null when the run did not record them (never zero). */
export interface AutomationSummary {
  latest_run: AnalysisStatusSummary;
  parent_revision_id: string | null;
  changed_file_count: number | null;
  changed_symbol_count: number | null;
  impacted_symbol_count: number | null;
  affected_file_count: number | null;
  limitation_count: number | null;
}

export interface RecentSession {
  session_id: string;
  title: string | null;
  status: string;
  last_active_at: string;
  message_count: number;
}

export interface RecentSessionSummary {
  session_count: number;
  recent_sessions: RecentSession[];
}

export interface RepositoryOverview {
  repository: RepositorySummary;
  revision: RevisionSummary;
  statistics: RepositoryStatistics;
  structure: RepositoryStructure;
  graph: GraphSummary;
  important_components: ImportantComponents;
  analysis_statuses: AnalysisStatusSummary[];
  security: SecuritySummary;
  testing: GeneratedTestSummary;
  documentation: DocumentationSummary;
  rag: RagSummary;
  automation: AutomationSummary;
  sessions: RecentSessionSummary;
  limitations: string[];
}

// ---------- Ingestion (POST /repositories/ingest) ----------

export interface IngestResponse {
  repository_id: string;
  revision_id: string;
  commit_sha: string;
  status: string;
  file_count: number;
}

// ---------- Impact (POST /repositories/{id}/impact) ----------

export interface ImpactRequest {
  revision_id: string;
  symbol_id?: string;
  qualified_name?: string;
  include_explanation: boolean;
}

/** `parent_symbol_id` is the containing symbol (e.g. a method's class), not a traversal predecessor. */
export interface ImpactedSymbol {
  symbol_id: string;
  qualified_name: string;
  symbol_kind: string;
  file_id: string;
  file_path: string;
  depth: number;
  relationship_types: string[];
  parent_symbol_id: string | null;
}

export interface ImpactedFile {
  file_id: string;
  path: string;
  symbol_count: number;
  minimum_depth: number;
}

export interface ImpactExplanation {
  summary: string;
  why_it_matters: string;
  affected_areas: string[];
  review_recommendations: string[];
  test_recommendations: string[];
  evidence: string[];
  limitations: string;
}

/** "deterministic" (no explanation requested) | "deterministic_only" (explanation unavailable) | "explained". */
export type ImpactAnalysisStatus =
  "deterministic" | "deterministic_only" | "explained";

export interface ImpactResponse {
  revision_id: string;
  target_symbol_id: string;
  target_qualified_name: string;
  direct_impacts: ImpactedSymbol[];
  transitive_impacts: ImpactedSymbol[];
  impacted_files: ImpactedFile[];
  max_depth: number;
  analysis_run_id: string | null;
  analysis_status: ImpactAnalysisStatus;
  explanation: ImpactExplanation | null;
  evidence: Record<string, EvidenceLocation> | null;
}

/** 409 detail body when a qualified name matches several symbols. */
export interface ImpactAmbiguity {
  message: string;
  candidates: { symbol_id: string; file_path: string; symbol_kind: string }[];
}

// ---------- Q&A (POST /repositories/{id}/qa) ----------

export interface QAEvidenceItem {
  evidence_id: string;
  file_path: string;
  symbol_id: string | null;
  qualified_name: string | null;
  line_start: number | null;
  line_end: number | null;
  retrieval_reason: string;
  retrieval_source: string;
  relationship_type: string | null;
}

export interface QAAnswer {
  answer: string;
  evidence: string[];
  limitations: string;
  related_symbols: string[];
  suggested_next_questions: string[];
}

export interface QAResponse {
  revision_id: string;
  question: string;
  analysis_status: "retrieval_only" | "answered";
  evidence_items: QAEvidenceItem[];
  answer: QAAnswer | null;
  evidence: Record<string, EvidenceLocation> | null;
}

// ---------- Security (POST /repositories/{id}/security/analyze) ----------

export type ScannerStatus =
  "completed" | "unavailable" | "timeout" | "malformed_output" | "error";

export interface ScannerResult {
  scanner: string;
  status: ScannerStatus;
  detail: string | null;
  finding_count: number;
  observation_count: number;
  scanner_version: string | null;
}

export interface Finding {
  finding_id: string;
  analyzer: string;
  rule_id: string;
  severity: string | null;
  title: string;
  message: string;
  status: string;
  cvss_score: number | null;
  cvss_vector: string | null;
  scanner_confidence: string | null;
  references: string[];
}

export interface Observation {
  observation_id: string;
  file_path: string | null;
  start_line: number | null;
  end_line: number | null;
  affected_package: string | null;
  installed_version: string | null;
  affected_range: string | null;
  fixed_version: string | null;
  resolution_status: string;
  symbol_id: string | null;
}

export interface BlastRadiusSymbol {
  symbol_id: string;
  qualified_name: string;
  kind: string;
  file_path: string;
  depth: number;
  relationship_types: string[];
}

export interface BlastRadius {
  seed_symbol_id: string;
  direct_dependents: BlastRadiusSymbol[];
  transitive_dependents: BlastRadiusSymbol[];
  impacted_files: ImpactedFile[];
  max_depth_reached: number;
  truncated: boolean;
}

export interface SecurityExplanation {
  summary: string;
  why_it_matters: string;
  blast_radius_interpretation: string;
  remediation_direction: string;
  evidence: string[];
  limitations: string;
}

export interface FindingWithContext {
  finding: Finding;
  observations: Observation[];
  blast_radius: BlastRadius | null;
  explanation: SecurityExplanation | null;
}

export interface SecurityAnalysisRequest {
  revision_id: string;
  include_explanation: boolean;
}

export interface SecurityAnalysisResponse {
  repository_id: string;
  revision_id: string;
  analysis_run_id: string | null;
  analysis_status: string;
  scanner_results: ScannerResult[];
  findings: FindingWithContext[];
  limitations: string[];
}

// ---------- Testing (POST /repositories/{id}/testing/generate) ----------

export interface GenerateTestsRequest {
  revision_id: string;
  symbol_ids: string[];
  file_ids: string[];
  execute: boolean;
}

export interface ExecutionSummary {
  status: string;
  exit_code: number | null;
  timed_out: boolean;
  duration_ms: number;
  stdout: string;
  stderr: string;
}

export interface GeneratedTestOutcome {
  target_symbol_id: string;
  target_qualified_name: string;
  selection_reason: string;
  status: string;
  generated_test_id: string | null;
  execution_id: string | null;
  validation_issues: { code: string; detail: string }[];
  execution: ExecutionSummary | null;
  error: string | null;
}

export interface GenerateTestsResponse {
  repository_id: string;
  revision_id: string;
  analysis_run_id: string | null;
  analysis_status: "no_targets" | "no_provider" | "completed" | string;
  outcomes: GeneratedTestOutcome[];
  limitations: string;
}

// ---------- Automation (POST /repositories/{id}/automation/analyze) ----------

/** A file in an automation revision delta. */
export interface DeltaFileEntry {
  path: string;
  file_id: string;
  content_hash: string;
}

export interface ModifiedFile {
  path: string;
  parent_content_hash: string;
  current_content_hash: string;
}

export interface FileDelta {
  added: DeltaFileEntry[];
  deleted: DeltaFileEntry[];
  modified: ModifiedFile[];
  added_count: number;
  deleted_count: number;
  modified_count: number;
  unchanged_count: number;
  rename_detection: string;
  truncated: boolean;
}

export interface SymbolSnapshot {
  file_path: string;
  kind: string;
  qualified_name: string;
  symbol_id: string;
}

export interface ModifiedSymbol {
  file_path: string;
  kind: string;
  qualified_name: string;
  parent_symbol_id: string;
  current_symbol_id: string;
}

export interface SymbolDelta {
  added: SymbolSnapshot[];
  deleted: SymbolSnapshot[];
  modified: ModifiedSymbol[];
  added_count: number;
  deleted_count: number;
  modified_count: number;
  unchanged_count: number;
  truncated: boolean;
}

/** impact_status: "computed" | "unavailable_deleted_symbol". */
export interface DeletedSymbolImpact {
  file_path: string;
  kind: string;
  qualified_name: string;
  symbol_id: string;
  impact_status: string;
  reason: string;
}

export interface AutomationImpactSummary {
  changed_symbol_count: number;
  direct: Omit<ImpactedSymbol, "file_id" | "parent_symbol_id">[];
  transitive: Omit<ImpactedSymbol, "file_id" | "parent_symbol_id">[];
  affected_files: ImpactedFile[];
  max_depth: number;
  truncated: boolean;
  deleted_symbol_impacts: DeletedSymbolImpact[];
}

/** execution_strategy is currently always "full_revision_rebuild". */
export interface StageExecution {
  stage: string;
  invalidated: boolean;
  execution_strategy: string;
  status: string;
  detail: string | null;
}

export interface RagInvalidation {
  status: string;
  chunks_invalidated: number;
  embeddings_require_regeneration: boolean;
  execution_strategy: string;
  detail: string | null;
}

export interface TestingInvalidation {
  stale_generated_test_ids: string[];
  changed_target_symbol_ids: string[];
  detail: string | null;
}

export interface SecurityInvalidation {
  rerun_required: boolean;
  reason: string;
  detail: string | null;
}

export interface ChangeSummary {
  files_added: number;
  files_modified: number;
  files_deleted: number;
  files_unchanged: number;
  symbols_added: number;
  symbols_modified: number;
  symbols_deleted: number;
  symbols_unchanged: number;
  directly_affected_symbols: number;
  transitively_affected_symbols: number;
  affected_files: number;
  chunks_invalidated: number;
  embeddings_require_regeneration: boolean;
  stale_generated_tests: number;
  changed_test_targets: number;
  security_rerun_required: boolean;
  deleted_symbols_with_unavailable_impact: number;
}

export interface AutomationResponse {
  repository_id: string;
  current_revision_id: string;
  parent_revision_id: string | null;
  analysis_run_id: string | null;
  analysis_status: string;
  file_delta: FileDelta;
  symbol_delta: SymbolDelta;
  parsing_execution: StageExecution;
  graph_execution: StageExecution;
  impact_summary: AutomationImpactSummary | null;
  rag_invalidation: RagInvalidation;
  testing_invalidation: TestingInvalidation;
  security_invalidation: SecurityInvalidation;
  summary: ChangeSummary;
  limitations: string[];
  execution_note: string;
}

// ---------- Documentation ----------

export type DocumentationArtifactType =
  | "repository_overview"
  | "architecture_overview"
  | "module_documentation"
  | "symbol_documentation";

/** generation_status: "generated" (LLM) | "fallback" (deterministic). */
export interface DocumentationArtifactSummary {
  artifact_id: string;
  repository_id: string;
  revision_id: string;
  artifact_type: string;
  target_file_id: string | null;
  target_symbol_id: string | null;
  title: string;
  generation_status: "generated" | "fallback" | string;
  created_at: string;
  updated_at: string;
}

export interface DocumentationArtifact extends DocumentationArtifactSummary {
  content: string;
  provider: string;
  model_name: string;
  model_version: string;
  evidence_ids: string[];
  cited_evidence_ids: string[];
  context_truncated: boolean;
  analysis_run_id: string;
}

export interface DocumentationListResponse {
  artifacts: DocumentationArtifactSummary[];
}

export interface GenerateDocumentationRequest {
  revision_id: string;
  artifact_type: DocumentationArtifactType;
  source_file_id?: string;
  symbol_id?: string;
}

// ---------- Investigation sessions ----------

export interface SessionSummary {
  id: string;
  title: string | null;
  status: string;
  started_at_revision_id: string | null;
  current_revision_id: string | null;
  created_at: string;
  last_active_at: string;
  message_count: number;
}

export interface RecentSessionsResponse {
  sessions: SessionSummary[];
  total_count: number;
  limit: number;
  offset: number;
}

export interface SessionMessage {
  id: string;
  session_id: string;
  role: string;
  content: string;
  sequence_number: number;
  created_at: string;
}

export interface ContextRef {
  id: string;
  session_id: string;
  message_id: string | null;
  ref_type:
    | "selected_file"
    | "selected_symbol"
    | "graph_focus"
    | "analysis_reference"
    | "finding_reference"
    | string;
  file_id: string | null;
  symbol_id: string | null;
  analysis_run_id: string | null;
  finding_id: string | null;
  graph_camera_state: Record<string, unknown> | null;
  created_at: string;
}

export interface SessionDetail {
  id: string;
  repository_id: string;
  title: string | null;
  status: string;
  started_at_revision_id: string | null;
  current_revision_id: string | null;
  created_at: string;
  last_active_at: string;
  messages: {
    messages: SessionMessage[];
    has_more: boolean;
    next_before_sequence: number | null;
  };
  context_refs: ContextRef[];
}

// ---------- Analysis pipeline (Product Integration v1) ----------

export type PipelineStageName =
  "parsing" | "graph" | "rag_chunking" | "rag_embedding";

/** AnalysisRun.status of the latest run, "not_run", or "unavailable". */
export type PipelineStageStatus = AnalysisRunStatus | "unavailable";

export interface StageAssessment {
  stage: PipelineStageName;
  status: PipelineStageStatus;
  /** Output exists and is not outdated by a later upstream rebuild. */
  up_to_date: boolean;
  analysis_run_id: string | null;
  started_at: string | null;
  finished_at: string | null;
  detail: string | null;
}

export interface PipelineStatus {
  repository_id: string;
  revision_id: string;
  stages: StageAssessment[];
  analysis_in_progress: boolean;
}

export interface PipelineStageResult extends StageAssessment {
  requested: boolean;
  /** "ran" | "reused" | "skipped" | "not_requested". */
  action: string;
  /** Set when the stage ran in this request and failed. */
  error: string | null;
}

export interface PipelineResult {
  repository_id: string;
  revision_id: string;
  /** "completed" | "partial" | "failed". */
  overall_status: string;
  stages: PipelineStageResult[];
  /** Always "synchronous": the request returns when the stages finish. */
  execution: string;
}

export interface AnalyzeRequest {
  run_parsing?: boolean;
  run_graph?: boolean;
  run_rag?: boolean;
  force?: boolean;
}

// ---------- Code intelligence reads ----------

export interface RevisionEntry {
  revision_id: string;
  commit_sha: string;
  parent_sha: string | null;
  branch_ref: string | null;
  committed_at: string;
  discovered_at: string;
  latest_analysis_status: string | null;
  file_count: number | null;
  is_current: boolean;
}

export interface RevisionPage {
  revisions: RevisionEntry[];
  total_count: number;
  limit: number;
  offset: number;
}

/** A stored source file of one revision (code intelligence reads). */
export interface RevisionFile {
  file_id: string;
  path: string;
  language: string | null;
  size_bytes: number;
  line_count: number | null;
  parsing_status: string;
}

export interface DirectoryListing {
  directory: string;
  directories: { name: string; path: string; file_count: number }[];
  directories_truncated: boolean;
  files: RevisionFile[];
  total_files: number;
  limit: number;
  offset: number;
}

export interface SymbolEntry {
  symbol_id: string;
  name: string;
  qualified_name: string;
  kind: string;
  file_id: string;
  file_path: string;
  start_line: number;
  end_line: number;
  parent_symbol_id: string | null;
  signature: string | null;
}

export interface FileDetail {
  file: RevisionFile;
  revision_id: string;
  symbols: SymbolEntry[];
  symbols_truncated: boolean;
  source_available: boolean;
}

export interface SourceRange {
  file_id: string;
  path: string;
  start_line: number;
  end_line: number;
  total_lines: number;
  lines: string[];
  truncated: boolean;
}

export interface SymbolSearchResult {
  symbols: SymbolEntry[];
  truncated: boolean;
}

export type GraphDirection = "dependents" | "dependencies" | "both";

export interface GraphNode {
  symbol_id: string;
  file_id: string;
  file_path: string;
  qualified_name: string;
  kind: string;
  depth: number;
  reached_via: "focus" | "dependents" | "dependencies" | "both";
}

/** source = dependent, target = dependency. */
export interface GraphEdge {
  source: string;
  target: string;
  relationship_type: string;
  occurrences: number;
}

export interface GraphNeighborhood {
  revision_id: string;
  focus_symbol_id: string;
  direction: GraphDirection;
  depth: number;
  node_limit: number;
  nodes: GraphNode[];
  edges: GraphEdge[];
  truncated: boolean;
  max_depth_reached: number;
  focus_external_references: string[];
}

export interface PersistedFinding {
  finding_id: string;
  analyzer: string;
  rule_id: string;
  severity: string | null;
  title: string;
  message: string;
  triage_status: string;
  cvss_score: number | null;
  observation_count: number;
  first_location: string | null;
}

export interface FindingPage {
  findings_run_id: string | null;
  findings: PersistedFinding[];
  total_count: number;
  limit: number;
  offset: number;
}

export interface PersistedGeneratedTest {
  generated_test_id: string;
  target_symbol_id: string;
  target_qualified_name: string | null;
  proposed_path: string;
  status: string;
  validation_status: string;
  provider: string;
  model_name: string;
  created_at: string;
  latest_execution: {
    execution_id: string;
    status: string;
    exit_code: number | null;
    timed_out: boolean;
    duration_ms: number | null;
    finished_at: string | null;
  } | null;
}

export interface GeneratedTestPage {
  tests: PersistedGeneratedTest[];
  total_count: number;
  limit: number;
  offset: number;
}

export interface ContextRefRequest {
  ref_type:
    | "selected_file"
    | "selected_symbol"
    | "graph_focus"
    | "analysis_reference"
    | "finding_reference";
  file_id?: string;
  symbol_id?: string;
  analysis_run_id?: string;
  finding_id?: string;
  graph_camera_state?: Record<string, unknown>;
}
