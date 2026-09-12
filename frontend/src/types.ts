export type EducationType = "higher" | "secondary_vocational" | "school";
export type Degree = "bachelor" | "master" | "postgraduate" | "specialist";
export type EmploymentType = string;
export type ProfileGender = "male" | "female";

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
type ProfileContacts = {
  phone?: string | null;
  email?: string | null;
  messengers: string[];
};
export type PersonalProfileData = {
  full_name?: string | null;
  gender?: ProfileGender | null;
  residence?: string | null;
  job_search_locations: string[];
  contacts: ProfileContacts;
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
export type JobSession = { guaranteed_application?: boolean; cover_letter_auto?: boolean; cover_letter_template?: string | null; cover_letter_max_words?: number | null; id: number; profile_id: number; adapter_id: string; desired_job_description?: string | null; application_limit?: number | null; status: string; counters: Record<string, number>; started_at: string | null; finished_at: string | null; stop_reason: string | null };
export type Vacancy = { id: number; session_id: number | null; title: string; company: string | null; url: string; state: string; site?: string; source?: string; status_changed_at?: string | null; data?: Record<string, unknown>; evaluation: Evaluation | null };
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
