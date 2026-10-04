export type ScoringCriterion = { key: string; title: string; description: string; max_points: number };
export type ScoreComponent = ScoringCriterion & {
  points: number;
  raw_points?: number | null;
  raw_max_points?: number | null;
  minimum_points?: number | null;
  minimum_failed?: boolean;
  explanation: string;
  evidence: string[];
};
export type CompactScoreBreakdown = Record<string, number | null>;
export type Evaluation = {
  decision: string;
  score: number;
  confidence: number;
  category: string;
  reason?: string;
  score_breakdown: ScoreComponent[] | CompactScoreBreakdown;
  data?: Record<string, unknown> | null;
};
export type VacancyEvaluationFields = {
  score?: number | null;
  decision?: string | null;
  confidence?: number | null;
  category?: string | null;
  reason?: string | null;
  score_breakdown?: ScoreComponent[] | CompactScoreBreakdown | null;
};
export type Adapter = { site_id: string; display_name: string; allowed_domains: string[] };
export type ModelGenerationEvent = { diagnostic_id: string | null; at: string };
export type ModelGenerationHealth = {
  healthy: boolean | null;
  success_count: number;
  failure_count: number;
  running: number;
  queued: number;
  last_success: ModelGenerationEvent | null;
  last_failure: ModelGenerationEvent | null;
};
export type ModelStatus = {
  connected: boolean;
  model_available: boolean;
  model: string;
  provider: string;
  message?: string;
  generation_health: ModelGenerationHealth;
};
export type JobSession = { guaranteed_application?: boolean; hirehi_pro_enabled?: boolean; cover_letter_auto?: boolean; cover_letter_template?: string | null; cover_letter_max_words?: number | null; id: number; adapter_id: string; resume_source_site?: string | null; desired_job_description?: string | null; application_limit?: number | null; status: string; stage?: string | null; stage_started_at?: string | null; last_progress_at?: string | null; wait_reason?: string | null; next_retry_at?: string | null; counters: Record<string, number>; started_at: string | null; finished_at: string | null; stop_reason: string | null };
export type SessionHistoryPage = { items: JobSession[]; total: number; limit: number; offset: number; has_more: boolean };
export type ResumeSnapshot = {
  session_id?: number;
  adapter_id?: string;
  source?: Record<string, unknown> | null;
  resume?: Record<string, unknown> | null;
  snapshot?: Record<string, unknown> | null;
  sections?: Array<Record<string, unknown>> | string[];
  contacts?: Record<string, unknown> | null;
  coverage?: Record<string, unknown> | null;
  import_url?: string | null;
  [key: string]: unknown;
};
export type ResumeAiContext = {
  model?: string;
  resume_context?: Record<string, unknown> | null;
  context?: Record<string, unknown> | null;
  [key: string]: unknown;
};
export type VacancyStatusGroup = "SUCCESS" | "PROCESSING" | "REJECTED" | "ERROR" | "CANCELLED";
export type Vacancy = VacancyEvaluationFields & { id: number; session_id: number | null; title: string; company: string | null; url: string; state: string; status_group?: VacancyStatusGroup; error_code?: string | null; error_message?: string | null; site?: string; source?: string; status_changed_at?: string | null; data?: Record<string, unknown>; evaluation: Evaluation | null };
export type VacancyPage = { items: Vacancy[]; total: number; limit: number; offset: number; has_more: boolean };
export type Notification = {
  id: number;
  event_id?: string | null;
  kind: string;
  title: string;
  message: string;
  source_type: string;
  source_id: string | null;
  target_path: string;
  read_at: string | null;
  created_at: string;
};
