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
export type Evaluation = {
  decision: string;
  score: number;
  confidence: number;
  category: string;
  reason: string;
  score_breakdown: ScoreComponent[];
};
export type Adapter = { site_id: string; display_name: string; allowed_domains: string[] };
export type JobSession = { guaranteed_application?: boolean; cover_letter_auto?: boolean; cover_letter_template?: string | null; cover_letter_max_words?: number | null; id: number; adapter_id: string; resume_source_site?: string | null; desired_job_description?: string | null; application_limit?: number | null; status: string; counters: Record<string, number>; started_at: string | null; finished_at: string | null; stop_reason: string | null };
export type VacancyStatusGroup = "SUCCESS" | "PROCESSING" | "REJECTED" | "ERROR" | "UNCONFIRMED";
export type Vacancy = { id: number; session_id: number | null; title: string; company: string | null; url: string; state: string; status_group?: VacancyStatusGroup; error_code?: string | null; error_message?: string | null; site?: string; source?: string; status_changed_at?: string | null; data?: Record<string, unknown>; evaluation: Evaluation | null };
export type VacancyPage = { items: Vacancy[]; total: number; limit: number; offset: number; has_more: boolean };
export type Notification = {
  id: number;
  kind: string;
  title: string;
  message: string;
  source_type: string;
  source_id: string | null;
  target_path: string;
  read_at: string | null;
  created_at: string;
};
