export type EducationType = "higher" | "secondary_vocational" | "school";
export type Degree = "bachelor" | "master" | "postgraduate" | "specialist";
export type EmploymentType = string;
export type WorkFormat = string;

export type Education = {
  type: EducationType;
  institution: string;
  faculty?: string | null;
  specialty?: string | null;
  degree?: Degree | null;
  start_date?: string | null;
  end_date?: string | null;
};
export type Language = { language: string; proficiency: string };
export type Contacts = {
  phone?: string | null;
  email?: string | null;
  messengers: string[];
};
export type PersonalProfileData = {
  full_name?: string | null;
  residence?: string | null;
  job_search_locations: string[];
  contacts: Contacts;
  education: Education[];
  languages: Language[];
  driver_license?: boolean | null;
  resumes?: Resume[];
};
export type Profile = {
  id: number;
  data: PersonalProfileData;
  created_at: string;
};
export type WorkExperience = {
  company: string;
  position: string;
  start_date?: string | null;
  end_date?: string | null;
  duties: string;
};
export type Resume = {
  id: number;
  profile_id: number;
  name: string;
  desired_title?: string | null;
  desired_salary?: string | null;
  employment_types: string[];
  work_formats: string[];
  business_trips?: boolean | null;
  experiences: WorkExperience[];
  skills: string[];
  about: string;
  selected_for_matching: boolean;
  original_filename?: string | null;
};
export type ScoringCriterion = { key: string; title: string; description: string; max_points: number };
export type ScoreComponent = ScoringCriterion & { points: number; explanation: string; evidence: string[] };
export type FlagMatch = {
  flag: string;
  confidence?: number | null;
  evidence?: string[];
  matched?: boolean | null;
};
export type PolicyFilter = {
  reason?: string | null;
  green_flags?: FlagMatch[];
  red_flags?: FlagMatch[];
  work_format?: { compatible?: boolean | null; confidence?: number | null; vacancy_format?: string | null; candidate_formats?: string[]; evidence?: string[] } | null;
};
export type Evaluation = {
  decision: string;
  score: number;
  confidence: number;
  category: string;
  reason: string;
  score_breakdown: ScoreComponent[];
  flag_filter?: PolicyFilter | null;
  work_format_match?: boolean | null;
  work_format?: { compatible?: boolean | null; confidence?: number | null; vacancy_format?: string | null; candidate_formats?: string[]; evidence?: string[] } | null;
};
export type CompiledPolicy = {
  request_text: string;
  score_threshold: number;
  scoring_criteria: ScoringCriterion[];
  green_flags?: string[];
  red_flags?: string[];
  flag_confidence_threshold?: number | null;
};
export type Policy = { id: number; request_text: string; score_threshold: number; compiled: CompiledPolicy; confirmed: boolean; flag_confidence_threshold?: number | null };
export type Adapter = { site_id: string; display_name: string; allowed_domains: string[]; safe_live_modes: string[] };
export type JobSession = { id: number; profile_id: number; adapter_id: string; mode: string; viewed_limit?: number | null; application_limit?: number | null; status: string; counters: Record<string, number>; started_at: string | null; finished_at: string | null; stop_reason: string | null };
export type Review = { id: number; session_id: number; vacancy_id: number | null; kind: string; question: string; status: string; answer: string | null };
export type Vacancy = { id: number; title: string; company: string | null; url: string; state: string; data: Record<string, unknown>; evaluation: Evaluation | null };
export type ReportSummary = { session_id: number; started_at: string | null; finished_at: string | null; stop_reason: string | null; adapter: string; mode: string; status: string; counters: Record<string, number>; aggregates?: { total: number; evaluated: number; matched: number; submitted: number; already_applied: number; review: number; errors: number }; vacancies: unknown[] };
export type Report = { id: number; session_id: number; summary: ReportSummary; created_at: string; pdf_url: string };
