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
  unsupported_fields?: string[];
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
  /** Safe import URL. HH/Zarplata may include only ?print=true. */
  import_url?: string | null;
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
  usesSavedData: boolean;
  resumeDataStatus: "ready" | "missing" | "corrupt" | null;
  resumeDataSavedAt: string | null;
  resumeDataErrorMessage: string | null;
  completionStatus: ResumeCompletionStatus | null;
  completionErrorMessage: string | null;
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
  /** Safe import URL returned by the server for the printable HTML. */
  importUrl?: string;
  importedAt?: string;
};

export type ResumeCompletionStatus = "empty" | "complete" | "partial" | "error";

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
      : false;
    return allowed && /^\/resume\/[^/]+\/?$/u.test(parsed.pathname) ? candidate : undefined;
  } catch {
    return undefined;
  }
}

/**
 * Validate the URL used to import/display a saved resume. This is separate
 * from the canonical profile URL: HH and Zarplata expose printable HTML only
 * with the exact `?print=true` query, while HireHi keeps its ordinary URL.
 */
export function safeResumeImportUrl(value: unknown, adapterId?: string, sourceUrl?: string): string | undefined {
  if (typeof value !== "string") return undefined;
  const candidate = value.trim();
  if (!candidate || !adapterId) return undefined;
  try {
    const parsed = new URL(candidate);
    if (parsed.protocol !== "https:" || parsed.username || parsed.password || parsed.port || parsed.hash) return undefined;
    const host = parsed.hostname.toLowerCase();
    const allowed = adapterId === "hh"
      ? host === "hh.ru" || host.endsWith(".hh.ru")
      : adapterId === "hirehi"
        ? host === "hirehi.ru" || host === "www.hirehi.ru"
        : adapterId === "zarplata"
          ? host === "zarplata.ru" || host.endsWith(".zarplata.ru")
          : false;
    if (!allowed || !/^\/resume\/[^/]+\/?$/u.test(parsed.pathname)) return undefined;
    if (sourceUrl) {
      const source = publicResumeSourceUrl(sourceUrl, adapterId);
      if (!source) return undefined;
      const sourceParsed = new URL(source);
      if (sourceParsed.hostname.toLowerCase() !== host || sourceParsed.pathname.replace(/\/$/u, "") !== parsed.pathname.replace(/\/$/u, "")) return undefined;
    }
    if (adapterId === "hirehi") return parsed.search ? undefined : candidate;
    return parsed.search === "?print=true" ? candidate : undefined;
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
      nested.importUrl = safeResumeImportUrl(value.import_url, nested.adapterId, nested.sourceUrl) ?? nested.importUrl;
    }
    return nested;
  }
  if (!isRecord(value) || typeof value.adapter_id !== "string") return null;
  const status = value.status === "changed" || value.status === "unavailable" ? value.status : "valid";
  const previewValue = isRecord(value.preview) ? value.preview : isRecord(value.public_preview) ? value.public_preview : null;
  const preview = previewValue ? sanitizePreviewValue(previewValue as ResumePreview) : {};
  const sourceUrl = publicResumeSourceUrl(value.source_url, value.adapter_id)
    ?? publicResumeSourceUrl(previewValue?.source_url, value.adapter_id);
  const importUrl = safeResumeImportUrl(value.import_url, value.adapter_id, sourceUrl)
    ?? safeResumeImportUrl(previewValue?.import_url, value.adapter_id, sourceUrl);
  const previewToken = typeof value.preview_token === "string" && value.preview_token ? value.preview_token : undefined;
  const grammaticalGender = value.grammatical_gender === "male" || value.grammatical_gender === "female"
    ? value.grammatical_gender
    : null;
  return {
    adapterId: value.adapter_id,
    usesSavedData: value.uses_saved_data === true || ["hh", "hirehi", "zarplata"].includes(value.adapter_id),
    // Older records predate durable resume copies. Treat them as missing so
    // they fail closed until the user explicitly refreshes them.
    resumeDataStatus: ["hh", "hirehi", "zarplata"].includes(value.adapter_id)
      ? value.resume_data_status === "ready" || value.resume_data_status === "corrupt" || value.resume_data_status === "missing"
        ? value.resume_data_status
        : "missing"
      : null,
    resumeDataSavedAt: typeof value.resume_data_saved_at === "string" ? value.resume_data_saved_at : null,
    resumeDataErrorMessage: typeof value.resume_data_error_message === "string" ? value.resume_data_error_message : null,
    completionStatus: value.completion_status === "empty" || value.completion_status === "complete" || value.completion_status === "partial" || value.completion_status === "error"
      ? value.completion_status
      : null,
    completionErrorMessage: typeof value.completion_error_message === "string" ? value.completion_error_message : null,
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
    importUrl,
  };
}

function sanitizePreviewValue(preview: ResumePreview): ResumePreview {
  const {
    source_resume_id: _sourceResumeId,
    source_edit_url: _sourceEditUrl,
    source_url: _sourceUrl,
    import_url: _importUrl,
    resume_url: _resumeUrl,
    url: _url,
    ...safePreview
  } = preview as ResumePreview & Record<string, unknown>;
  void _sourceResumeId;
  void _sourceEditUrl;
  void _sourceUrl;
  void _importUrl;
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
    const readableLabel = resumeSectionLabel(label);
    if (!result.some((item) => item.label === readableLabel && item.status === status)) result.push({ label: readableLabel, status });
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

const RESUME_SECTION_LABELS: Record<string, string> = {
  about: "О себе",
  identity: "Личные данные",
  personal: "Личные данные",
  personal_info: "Личные данные",
  experience: "Опыт работы",
  work_experience: "Опыт работы",
  education: "Образование",
  skills: "Навыки",
  key_skills: "Ключевые навыки",
  contacts: "Контакты",
  contact: "Контакты",
  languages: "Языки",
  certificates: "Сертификаты",
  portfolio: "Портфолио",
  recommendations: "Рекомендации",
  achievements: "Достижения",
  additional_info: "Дополнительная информация",
  citizenship: "Гражданство",
  relocation: "Готовность к переезду",
  schedule: "График работы",
  salary: "Зарплатные ожидания",
  desired_position: "Желаемая должность",
  target: "Желаемая должность",
  location: "Местоположение",
  total_experience: "Общий опыт работы",
};

const MAIN_RESUME_SECTIONS = new Set([
  "identity", "contacts", "target", "location", "experience", "skills", "education", "languages", "about", "total_experience",
]);
const MAIN_SECTION_ALIASES: Record<string, string> = {
  personal: "identity", personal_info: "identity", personal_data: "identity", identity: "identity",
  contact: "contacts", contacts: "contacts",
  desired_position: "target", target_role: "target", target_title: "target", target: "target",
  work_location: "location", location: "location",
  work_experience: "experience", experience: "experience",
  key_skills: "skills", skills: "skills",
  languages: "languages", education: "education", about: "about", total_experience: "total_experience",
};

function canonicalMainSection(value: string): string | undefined {
  const key = value.trim().toLocaleLowerCase().replace(/[\s-]+/gu, "_").split(/[.[\]]/u, 1)[0];
  const canonical = MAIN_SECTION_ALIASES[key];
  return canonical && MAIN_RESUME_SECTIONS.has(canonical) ? canonical : undefined;
}

/** Derive a compatibility status for older source responses without completion_status. */
export function completionStatusFromCoverage(preview: ResumePreview, resumeDataStatus: ResumeSourceRecord["resumeDataStatus"]): ResumeCompletionStatus {
  if (resumeDataStatus === "missing" || resumeDataStatus === "corrupt") return "error";
  if (resumeDataStatus !== "ready") return "empty";

  const sectionStatuses = new Map<string, string>();
  const add = (value: unknown, status: string) => {
    if (typeof value !== "string") return;
    const section = canonicalMainSection(value);
    if (section) sectionStatuses.set(section, status);
  };
  preview.sections?.forEach((item) => typeof item === "string" ? add(item, "present") : add(item.key || item.title || item.label, item.status || "present"));
  const coverage = preview.coverage;
  (coverage?.sections ?? []).forEach((item) => add(item, "present"));
  (coverage?.present_sections ?? coverage?.present ?? []).forEach((item) => add(item, "present"));
  (coverage?.missing_sections ?? coverage?.missing ?? []).forEach((item) => add(item, "not_provided"));
  (coverage?.hidden_fields ?? coverage?.hidden ?? preview.hidden_fields ?? []).forEach((item) => add(item, "hidden"));
  (coverage?.unsupported ?? []).forEach((item) => add(item, "unsupported"));
  (coverage?.unsupported_fields ?? []).forEach((item) => add(item, "unsupported"));
  (coverage?.parse_errors ?? []).forEach((item) => add(item, "parse_error"));

  const allPresent = [...MAIN_RESUME_SECTIONS].every((section) => sectionStatuses.get(section) === "present");
  return allPresent ? "complete" : "partial";
}

/** Avoid rendering URLs or URL credentials in profile status tooltips. */
export function safeCompletionErrorMessage(value: unknown): string | undefined {
  if (typeof value !== "string" || !value.trim()) return undefined;
  const safe = value.trim()
    .replace(/https?:\/\/[^\s)\]}>,]+/giu, "ссылку")
    .replace(/(?:www\.)?[\w.-]+\.(?:ru|com|net|org)\/[^\s)\]}>,]*/giu, "ссылку")
    .replace(/[?#][^\s)\]}>,]*/gu, "")
    .trim();
  return safe || "Не удалось обработать сохранённое резюме.";
}

export function resumeCompletionStatus(record: ResumeSourceRecord | undefined, importError?: string): ResumeCompletionStatus {
  if (!record) return importError ? "error" : "empty";
  if (record.resumeDataStatus === "missing" || record.resumeDataStatus === "corrupt") return "error";
  return record.completionStatus ?? completionStatusFromCoverage(record.preview, record.resumeDataStatus);
}

/** Turn known normalized source keys into labels suitable for the profile UI. */
export function resumeSectionLabel(value: string): string {
  const key = value.trim().toLocaleLowerCase().replace(/[\s-]+/gu, "_");
  const known = RESUME_SECTION_LABELS[key];
  if (known) return known;
  // Preserve already readable source-provided labels, while keeping opaque
  // machine keys out of the interface.
  if (/[А-Яа-яЁё]/u.test(value) || /\s/u.test(value)) return value;
  return "Другие сведения";
}

const RESUME_CONTACT_LABELS: Record<string, string> = {
  email: "Электронная почта",
  phone: "Телефон",
  cellphone: "Телефон",
  mobile: "Телефон",
  telegram: "Telegram",
  whatsapp: "WhatsApp",
  skype: "Skype",
  website: "Сайт",
  site: "Сайт",
  linkedin: "LinkedIn",
  github: "GitHub",
};

export function resumeContactLabel(value: string): string {
  const key = value.trim().toLocaleLowerCase().replace(/[\s-]+/gu, "_");
  const known = RESUME_CONTACT_LABELS[key];
  if (known) return known;
  if (/[А-Яа-яЁё]/u.test(value) || /\s/u.test(value)) return value;
  return "Другой контакт";
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
