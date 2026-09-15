export type ResumeFieldStatus = "present" | "not_provided" | "hidden" | "unsupported" | "parse_error" | string;

export type ResumeCoverage = {
  sections?: string[];
  present_sections?: string[];
  present?: string[];
  missing?: string[];
  missing_sections?: string[];
  hidden?: string[];
  hidden_fields?: string[];
  unsupported?: string[];
  parse_errors?: string[];
  present_count?: number;
  total_count?: number;
};

export type ResumePreview = {
  adapter_id?: string;
  source_site?: string;
  source_resume_id?: string;
  title?: string | null;
  target_role?: string | null;
  target_title?: string | null;
  // Optional site-provided value. It is displayed only when explicitly
  // present; the client must never infer grammatical gender from a name.
  gender?: string | null;
  grammatical_gender?: string | null;
  source_updated_at?: string | null;
  coverage?: ResumeCoverage | null;
  sections?: Array<{ key?: string; title?: string; label?: string; status?: ResumeFieldStatus; count?: number }> | string[];
  hidden_fields?: string[];
  missing_fields?: string[];
  model_fields?: string[];
  excluded_fields?: string[];
  data_sent_to_model?: string[];
  contacts?: { found?: string[]; hidden?: string[]; count?: number } | null;
  private_fields_found?: Record<string, boolean>;
  questions?: ResumeQuestion[];
  required_questions?: ResumeQuestion[];
  consent_required?: boolean;
  source_edit_url?: string | null;
  /** Full public URL returned by the server for a saved source. */
  source_url?: string | null;
  masked_url?: string | null;
};

export type ResumeQuestion = {
  id: string;
  question: string;
  reason?: string;
  options?: string[];
  required?: boolean;
};

export type ResumePreviewResponse = {
  preview_token: string;
  preview?: ResumePreview;
  public_preview?: ResumePreview;
  source?: ResumePreview;
  [key: string]: unknown;
};

export type ResumeSourceRecord = {
  adapterId: string;
  grammaticalGender?: "male" | "female" | null;
  previewToken?: string;
  preview: ResumePreview;
  confirmed: boolean;
  status: "valid" | "changed" | "unavailable";
  checkedAt?: string;
  changed?: boolean;
  maskedUrl?: string;
  /** Full public, allowlisted URL returned by the server. */
  sourceUrl?: string;
  importedAt?: string;
};

const STORAGE_KEY = "job-orchestrator.resume-sources";

export const RESUME_SITES = [
  { id: "hh", label: "HH.ru", editLabel: "Изменить резюме на HH.ru" },
  { id: "hirehi", label: "HireHi", editLabel: "Изменить резюме на HireHi" },
  { id: "zarplata", label: "Zarplata.ru", editLabel: "Изменить резюме на Zarplata.ru" },
] as const;

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object";
}

export function previewFromResponse(result: ResumePreviewResponse): ResumePreview {
  const candidate = isRecord(result.preview) ? result.preview : isRecord(result.public_preview) ? result.public_preview : isRecord(result.source) ? result.source : result;
  return candidate as ResumePreview;
}

/** Pick only the main desired position for the compact source card. */
export function resumeDisplayTitle(preview: ResumePreview): string {
  const raw = [preview.target_title, preview.target_role].find((value) => typeof value === "string" && value.trim())?.trim() ?? "";
  const firstLine = raw.split(/\r?\n/u).map((line) => line.trim()).find(Boolean) ?? "";
  const cleaned = firstLine
    .replace(/(?:^|\s)(?:Специализация(?:и)?|Тип занятости|Занятость|Формат работы|График работы)\s*[:—-]?.*$/iu, "")
    .replace(/[,:;—–-]\s*$/u, "")
    .trim();
  return cleaned || "Должность не указана";
}

export function maskResumeUrl(value: string): string {
  try {
    const url = new URL(value);
    const parts = url.pathname.split("/").filter(Boolean);
    const last = parts.pop();
    if (last) parts.push(`${last.slice(0, 3)}•••`);
    return `${url.hostname}/${parts.join("/")}`;
  } catch {
    return "Ссылка проверена";
  }
}

export function safeResumeUrlLabel(value?: string | null): string {
  if (!value || value === "[link omitted]") return "Ссылка сохранена";
  // Never render a value that still looks like a bearer URL. Keep already
  // masked labels (used for the session-scoped browser state) as-is.
  if (value.includes("://") || /[?#&]/u.test(value)) return maskResumeUrl(value);
  return value.includes("•••") ? value : maskResumeUrl(value);
}

/**
 * Keep the public source URL intact for the profile card while rejecting
 * values that could turn into a credential-bearing or non-HTTPS link.
 * The server is the allowlist authority; this is only a rendering guard.
 */
export function publicResumeSourceUrl(value: unknown, adapterId?: string): string | undefined {
  if (typeof value !== "string") return undefined;
  const candidate = value.trim();
  if (!candidate) return undefined;
  try {
    const parsed = new URL(candidate);
    if (parsed.protocol !== "https:" || parsed.username || parsed.password || parsed.port || parsed.search || parsed.hash) return undefined;
    const host = parsed.hostname.toLowerCase();
    const allowed = adapterId === "hh"
      ? host === "hh.ru" || host.endsWith(".hh.ru")
      : adapterId === "hirehi"
        ? host === "hirehi.ru" || host === "www.hirehi.ru"
        : adapterId === "zarplata"
          ? host === "zarplata.ru" || host.endsWith(".zarplata.ru")
          : true;
    return allowed && /^\/resume\/[^/]+\/?$/u.test(parsed.pathname) ? candidate : undefined;
  } catch {
    return undefined;
  }
}

export function purgeLegacyResumeSourceStorage(): void {
  // Browser storage is never an authority for sources and must not retain
  // bearer URLs or preview tokens. Remove only the exact legacy key so other
  // application state remains untouched.
  try { sessionStorage.removeItem(STORAGE_KEY); } catch { /* storage can be unavailable */ }
  try { localStorage.removeItem(STORAGE_KEY); } catch { /* storage can be unavailable */ }
}

export function sourceRecordFromResponse(value: unknown): ResumeSourceRecord | null {
  if (isRecord(value) && (isRecord(value.source) || isRecord(value.resume_source))) {
    const nested = sourceRecordFromResponse(value.source ?? value.resume_source);
    if (nested) {
      if (!nested.previewToken && typeof value.preview_token === "string") nested.previewToken = value.preview_token;
      nested.sourceUrl = publicResumeSourceUrl(value.source_url, nested.adapterId) ?? nested.sourceUrl;
    }
    return nested;
  }
  if (!isRecord(value) || typeof value.adapter_id !== "string") return null;
  const status = value.status === "changed" || value.status === "unavailable" ? value.status : "valid";
  const previewValue = isRecord(value.preview) ? value.preview : isRecord(value.public_preview) ? value.public_preview : null;
  const preview = previewValue ? sanitizePreviewValue(previewValue as ResumePreview) : {};
  const sourceUrl = publicResumeSourceUrl(value.source_url, value.adapter_id)
    ?? publicResumeSourceUrl(previewValue?.source_url, value.adapter_id);
  const previewToken = typeof value.preview_token === "string" && value.preview_token ? value.preview_token : undefined;
  const grammaticalGender = value.grammatical_gender === "male" || value.grammatical_gender === "female"
    ? value.grammatical_gender
    : null;
  return {
    adapterId: value.adapter_id,
    grammaticalGender,
    status,
    // The record's presence means it was durably confirmed. Usability is
    // represented separately by status and the optional fresh token.
    confirmed: true,
    preview,
    previewToken,
    checkedAt: typeof value.checked_at === "string" ? value.checked_at : undefined,
    changed: value.changed === true || status === "changed",
    // Keep only a presentation-safe label in React state. A server may send
    // either an already masked value or a legacy URL-shaped value here.
    maskedUrl: typeof value.masked_url === "string" ? safeResumeUrlLabel(value.masked_url) : undefined,
    sourceUrl,
  };
}

function sanitizePreviewValue(preview: ResumePreview): ResumePreview {
  const {
    source_resume_id: _sourceResumeId,
    source_edit_url: _sourceEditUrl,
    source_url: _sourceUrl,
    resume_url: _resumeUrl,
    url: _url,
    ...safePreview
  } = preview as ResumePreview & Record<string, unknown>;
  void _sourceResumeId;
  void _sourceEditUrl;
  void _sourceUrl;
  void _resumeUrl;
  void _url;
  if (typeof safePreview.masked_url === "string") safePreview.masked_url = safeResumeUrlLabel(safePreview.masked_url);
  return safePreview;
}

export function resumeSourcesFromResponse(value: unknown): Record<string, ResumeSourceRecord> {
  // Remove the legacy browser authority whenever the server source list is
  // loaded; this is a one-way migration and never reads the old value.
  purgeLegacyResumeSourceStorage();
  const raw = Array.isArray(value) ? value : isRecord(value) && Array.isArray(value.sources) ? value.sources : [];
  return Object.fromEntries(raw.map((item) => sourceRecordFromResponse(item)).filter((item): item is ResumeSourceRecord => Boolean(item)).map((item) => [item.adapterId, item]));
}

export function previewSections(preview: ResumePreview): Array<{ label: string; status: string }> {
  const result: Array<{ label: string; status: string }> = [];
  const add = (label: string, status: string) => {
    if (!result.some((item) => item.label === label && item.status === status)) result.push({ label, status });
  };
  preview.sections?.forEach((item) => typeof item === "string"
    ? add(item, "present")
    : add(item.title || item.label || item.key || "Раздел", item.status || "present"));
  const coverage = preview.coverage;
  (coverage?.present_sections ?? coverage?.present ?? []).forEach((label) => add(label, "present"));
  (coverage?.hidden_fields ?? coverage?.hidden ?? preview.hidden_fields ?? []).forEach((label) => add(label, "hidden"));
  (coverage?.missing_sections ?? coverage?.missing ?? preview.missing_fields ?? []).forEach((label) => add(label, "not_provided"));
  (coverage?.unsupported ?? []).forEach((label) => add(label, "unsupported"));
  (coverage?.parse_errors ?? []).forEach((label) => add(label, "parse_error"));
  return result;
}

/** Return the user-facing questions requested by the resume preview. */
export function resumeQuestions(preview: ResumePreview): ResumeQuestion[] {
  const questions = preview.required_questions ?? preview.questions ?? [];
  return Array.isArray(questions) ? questions : [];
}

export function statusLabel(status: string): string {
  return ({ present: "Найдено", hidden: "Скрыто на сайте", not_provided: "Не указано", unsupported: "Не поддерживается", parse_error: "Не удалось прочитать" } as Record<string, string>)[status] || status;
}

export function questionOptionLabel(question: ResumeQuestion, option: string): string {
  if (question.id === "grammatical_gender") {
    return ({ male: "Мужской", female: "Женский" } as Record<string, string>)[option] || option;
  }
  return option;
}

export function resumeGenderLabel(value: string): string {
  return ({ male: "Мужской", female: "Женский" } as Record<string, string>)[value] || value;
}

export function isResumeQuestionValid(question: ResumeQuestion, value: string): boolean {
  const answer = value.trim();
  if (!answer) return question.required === false;
  return !question.options?.length || question.options.includes(answer);
}
