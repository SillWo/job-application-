import { ReactNode, useEffect, useId, useRef, useState } from "react";
import { NavLink, Route, Routes, useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Toaster, toast } from "sonner";
import { Popover } from "@base-ui/react/popover";
import { Select } from "@base-ui/react/select";
import { api } from "./api";
import { SessionQuestions } from "./SessionQuestions";
import { useSessionDraft } from "./useSessionDraft";
import type {
  Adapter,
  Degree,
  Education,
  EducationType,
  EmploymentType,
  JobSession,
  Language,
  PersonalProfileData,
  Profile,
  Resume,
  ScoreComponent,
  Vacancy,
  VacancyPage,
  WorkExperience,
  Notification,
} from "./types";

const RELEVANCE_CRITERIA: ReadonlyArray<{ key: string; title: string; maxPoints: number; weight: number }> = [
  { key: "tasks", title: "Задачи", maxPoints: 4, weight: 35 },
  { key: "skills", title: "Навыки", maxPoints: 2, weight: 20 },
  { key: "experience_depth", title: "Годы опыта", maxPoints: 4, weight: 15 },
  { key: "role_match", title: "Роль", maxPoints: 4, weight: 10 },
  { key: "industry", title: "Сфера", maxPoints: 4, weight: 10 },
  { key: "special_requirements", title: "Особые требования", maxPoints: 2, weight: 10 },
];
const VACANCY_FILTER_CRITERIA = [
  { key: "tasks", title: "Задачи", max: 35 },
  { key: "skills", title: "Навыки", max: 20 },
  { key: "experience_depth", title: "Опыт", max: 15 },
  { key: "role_match", title: "Роль", max: 10 },
  { key: "industry", title: "Сфера", max: 10 },
  { key: "special_requirements", title: "Особые требования", max: 10 },
] as const;

const INFLUENCE_CRITERIA = [
  { key: "tasks", title: "Задачи", levels: ["Низкий", "Средний", "Высокий", "Максимальный"], hint: `ИИ оценивает сходство задач и обязанностей из вакансии с вашим резюме. Чем выше фактор — тем выше должно быть сходство, иначе REJECT!*\n* — ИИ на вакансию отклик не отправит` },
  { key: "skills", title: "Навыки", levels: ["Низкий", "Высокий"], hint: `ИИ оценивает насколько ваш набор навыков соответствует требованиям вакансии. Чем выше фактор — тем выше должно быть сходство, иначе REJECT!*\n* — ИИ на вакансию отклик не отправит` },
  { key: "experience_depth", title: "Годы опыта", levels: ["Низкий", "Средний", "Высокий", "Максимальный"], hint: `ИИ оценивает уровень и глубину подтверждённого опыта. Чем выше фактор — тем выше должно быть соответствие, иначе REJECT!*\n* — ИИ на вакансию отклик не отправит` },
  { key: "role_match", title: "Роль", levels: ["Низкий", "Средний", "Высокий", "Максимальный"], hint: `ИИ оценивает фактическое соответствие прошлой роли новой, а не только название должности. Чем выше фактор — тем выше должно быть соответствие, иначе REJECT!*\n* — ИИ на вакансию отклик не отправит` },
  { key: "industry", title: "Сфера", levels: ["Низкий", "Средний", "Высокий", "Максимальный"], hint: `ИИ оценивает сходство вакансии и ваших прошлых мест работы по сфере. Чем выше фактор — тем выше должно быть сходство, иначе REJECT!*\n* — ИИ на вакансию отклик не отправит` },
] as const;
const INFLUENCE_LEVELS = ["low", "medium", "high", "maximum"] as const;

function presentationBreakdown(rows: ScoreComponent[]): ScoreComponent[] {
  return RELEVANCE_CRITERIA.map((criterion) => {
    const legacyKey = criterion.key === "experience_depth" ? "required_years" : criterion.key === "role_match" ? "title" : criterion.key === "special_requirements" ? "languages" : null;
    const row = rows.find((candidate) => candidate.key === criterion.key) ?? (legacyKey ? rows.find((candidate) => candidate.key === legacyKey) : undefined);
    const hasRawScore = typeof row?.raw_points === "number" && typeof row?.raw_max_points === "number";
    const points = hasRawScore
      ? Math.min(criterion.maxPoints, Math.max(0, row!.raw_points! / (row!.raw_max_points! || 1) * criterion.maxPoints))
      : Math.round(Math.min(1, Math.max(0, (row?.points ?? 0) / (row?.max_points || 1))) * criterion.maxPoints);
    const rawPoints = hasRawScore
      ? Math.min(criterion.maxPoints, Math.max(0, row!.raw_points! / (row!.raw_max_points! || 1) * criterion.maxPoints))
      : points;
    return {
      ...(row ?? { key: criterion.key, description: "", explanation: "", evidence: [] }),
      key: criterion.key,
      title: criterion.title,
      max_points: criterion.weight,
      points: Math.round((rawPoints / criterion.maxPoints) * criterion.weight),
      raw_points: rawPoints,
      raw_max_points: criterion.maxPoints,
    };
  });
}

const nav = [["/", "Обзор", "M4 12h16M12 4l8 8-8 8"], ["/profile", "Профиль", "M20 21a8 8 0 0 0-16 0M12 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8"], ["/session", "Сессия", "M4 6h16M4 12h16M4 18h16"], ["/vacancies", "Вакансии", "M6 3h9l3 3v15H6zM9 12h6M9 16h6"], ["/model", "Модель", "M4 6h16M4 12h16M4 18h16M8 4v4m8 2v4m-5 4v4"]] as const;
const STATUS_META: Record<string, { label: string; tone: string }> = {
  CREATED: { label: "Создана", tone: "neutral" }, RUNNING: { label: "В работе", tone: "success" }, DISCOVERED: { label: "Найдена", tone: "neutral" }, EXTRACTED: { label: "Данные получены", tone: "neutral" }, EVALUATING: { label: "Оценка вакансии", tone: "info" }, WAITING_FOR_LOGIN: { label: "Ожидает входа", tone: "warning" }, PAUSED: { label: "Приостановлена", tone: "warning" }, STOPPED: { label: "Остановлена", tone: "neutral" }, COMPLETED: { label: "Завершена", tone: "success" }, FAILED: { label: "Ошибка", tone: "danger" }, UNKNOWN: { label: "Ошибка", tone: "danger" }, UNKNOWN_RESULT: { label: "Ошибка", tone: "danger" }, REJECTED_BY_MODEL: { label: "Отклонена моделью", tone: "danger" }, FILTERED_OUT: { label: "Отклонена моделью", tone: "danger" }, ERROR: { label: "Ошибка", tone: "danger" }, READY_TO_SUBMIT: { label: "Готова к отклику", tone: "success" }, READY_TO_REPORT: { label: "Готова к отчёту", tone: "success" }, SUBMITTED: { label: "Отклик отправлен", tone: "success" }, REPORTED: { label: "В отчёте", tone: "success" }, ALREADY_APPLIED: { label: "Отклик отправлен", tone: "success" }, CONTACT_COLLECTED: { label: "Контакт получен", tone: "neutral" }, NEEDS_REVIEW: { label: "Требует проверки", tone: "warning" }, SKIPPED_TEST: { label: "Тест пропущен", tone: "neutral" }, LETTER_GENERATED: { label: "Письмо подготовлено", tone: "neutral" }, FILLING_FORM: { label: "Заполнение формы", tone: "info" }, SUBMITTING: { label: "Отправка отклика", tone: "info" }, CONNECTED: { label: "Соединение есть", tone: "success" }, DISCONNECTED: { label: "Нет соединения", tone: "danger" },
};
type VacancyFilters = {
  search: string;
  state: string;
  site: string;
  status_date_from: string;
  status_date_to: string;
  total_score_min: string;
  total_score_max: string;
  sort: string;
  sort_dir: string;
  tasks_min: string;
  tasks_max: string;
  skills_min: string;
  skills_max: string;
  experience_depth_min: string;
  experience_depth_max: string;
  role_match_min: string;
  role_match_max: string;
  industry_min: string;
  industry_max: string;
  special_requirements_min: string;
  special_requirements_max: string;
};
const DEFAULT_VACANCY_FILTERS: VacancyFilters = {
  search: "",
  state: "",
  site: "",
  status_date_from: "",
  status_date_to: "",
  total_score_min: "",
  total_score_max: "",
  sort: "date",
  sort_dir: "desc",
  tasks_min: "",
  tasks_max: "",
  skills_min: "",
  skills_max: "",
  experience_depth_min: "",
  experience_depth_max: "",
  role_match_min: "",
  role_match_max: "",
  industry_min: "",
  industry_max: "",
  special_requirements_min: "",
  special_requirements_max: "",
};

function buildVacancyParams(
  filters: VacancyFilters,
  includePaging = false,
  offset = 0,
) {
  const params = new URLSearchParams();
  Object.entries(filters).forEach(([key, value]) => {
    const isDefaultSort = key === "sort" && value === DEFAULT_VACANCY_FILTERS.sort;
    const isDefaultDirection = key === "sort_dir" && value === DEFAULT_VACANCY_FILTERS.sort_dir;
    if (value && !isDefaultSort && !isDefaultDirection) params.set(key, value);
  });
  if (includePaging && offset > 0) {
    params.set("limit", "30");
    params.set("offset", String(offset));
  }
  return params;
}

function vacancyExportUrl(filters: VacancyFilters, format: "csv" | "xlsx" | "xml") {
  const params = buildVacancyParams(filters);
  params.set("format", format);
  return `/api/vacancies/export?${params}`;
}
function humanStatus(value: string) { return STATUS_META[value]?.label ?? value.replaceAll("_", " ").toLowerCase(); }
const VACANCY_STATUS_OPTIONS = [
  { value: "EVALUATING", label: "Оценка вакансии" }, { value: "REJECTED_BY_MODEL", label: "Отклонена моделью" }, { value: "REPORTED", label: "В отчёте" }, { value: "ERROR", label: "Ошибка" },
] as const;
function vacancyOutcome(value: string) {
  if (value === "SUBMITTED") return "Отклик действительно отправлен после положительной оценки вакансии.";
  if (value === "ALREADY_APPLIED") return "Новый отклик не отправлялся: вы уже откликались на эту вакансию.";
  if (value === "REJECTED_BY_MODEL" || value === "FILTERED_OUT") return "Модель отклонила вакансию из-за недостаточной релевантности, поэтому отклик не отправлен.";
  if (["ERROR", "FAILED", "UNKNOWN", "UNKNOWN_RESULT"].includes(value)) return "Отклик не отправлен из-за ошибки обработки вакансии.";
  if (value === "REPORTED") return "Вакансия добавлена в отчёт, внешний отклик не отправлялся.";
  return "Обработка вакансии ещё идёт.";
}
function humanRelevanceReason(reason: string, state: string) {
  const cleaned = reason.replace(/\s*(?:Ограничения|Restrictions)\s*:.*/is, "").replace(/\s*(?:Evidence не прошло локальную лексическую проверку|grounding warning).*/i, "").trim();
  const legacyGeneric = cleaned === "Оценка вакансии на основе резюме.";
  if (!cleaned || legacyGeneric || /(?:минимум|minimum|score|confidence|evidence|threshold|flag|raw[_ ]?points|raw[_ ]?fields|grounding|локальную лексическую проверку)/i.test(cleaned) || /\d+\s*\/\s*\d+/.test(cleaned)) {
    if (["SUBMITTED", "ALREADY_APPLIED", "REPORTED"].includes(state)) return "Вакансия в целом соответствует вашему профилю.";
    if (["ERROR", "FAILED"].includes(state)) return "Вакансия оценена по резюме, но обработка не завершилась.";
    return ["REJECTED_BY_MODEL", "FILTERED_OUT"].includes(state) ? "Вакансия недостаточно релевантна вашему профилю." : "Вакансия оценена по вашему резюме.";
  }
  return cleaned;
}
function humanCriterionExplanation(explanation: string) {
  const cleaned = explanation.replace(/\s*(?:Evidence не прошло локальную лексическую проверку|grounding warning).*/i, "").trim();
  return !cleaned || /(?:минимум|minimum|score|confidence|evidence|threshold|flag|raw[_ ]?points|raw[_ ]?fields|grounding)/i.test(cleaned) || /\d+\s*\/\s*\d+/.test(cleaned) ? "По этому критерию дополнительных пояснений нет." : cleaned;
}
function humanModelSummary(reason: string, state: string, rows: ScoreComponent[]) {
  const cleanedReason = humanRelevanceReason(reason, state);
  const legacy = reason.trim() === "Оценка вакансии на основе резюме." || cleanedReason === "Вакансия оценена по вашему резюме." || cleanedReason === "Вакансия недостаточно релевантна вашему профилю.";
  if (!legacy) return cleanedReason;
  const meaningful = rows
    .filter((row) => ["tasks", "skills", "experience_depth", "required_years", "role_match", "industry", "languages", "special_requirements"].includes(row.key))
    .map((row) => ({ row, text: humanCriterionExplanation(row.explanation) }))
    .filter(({ text }) => text !== "По этому критерию дополнительных пояснений нет.")
    .sort((a, b) => {
      const ratio = (item: typeof a) => (typeof item.row.raw_points === "number" && typeof item.row.raw_max_points === "number") ? item.row.raw_points / (item.row.raw_max_points || 1) : item.row.points / (item.row.max_points || 1);
      return ratio(a) - ratio(b);
    });
  const firstSentence = (text: string) => {
    const sentence = text.match(/^.*?(?:[.!?](?=\s+[А-ЯЁA-Z]|$))/u)?.[0]?.trim() ?? text.trim();
    return sentence ? (/[.!?]$/u.test(sentence) ? sentence : `${sentence}.`) : "";
  };
  const tasks = meaningful.find(({ row }) => row.key === "tasks");
  const selected = tasks ? [tasks, ...meaningful.filter(({ row }) => row.key !== "tasks").slice(0, 1)] : meaningful.slice(0, 2);
  const sentences = selected.map(({ text }) => firstSentence(text)).filter(Boolean);
  return sentences.length ? sentences.join(" ") : cleanedReason;
}

function Shell({ children }: { children: ReactNode }) {
  return (
    <div className="shell">
      <header className="topbar"><div className="topbar-inner">
        <div className="brand">
          <span className="brandmark">J</span>
          <div>
            <strong>Job Orchestrator</strong>
            <small>локальный агент</small>
          </div>
        </div>
        <nav className="topbar-nav" aria-label="Основная навигация">
          {nav.map(([to, label, path]) => (
            <NavLink key={to} to={to} end={to === "/"}>
              <svg className="topbar-nav-icon" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d={path} /></svg>
              {label}
            </NavLink>
          ))}
        </nav>
        <div className="topbar-actions"><Notifications /><a href="/docs" target="_blank">API</a></div></div></header>
      <main>{children}
      </main>
      <Toaster position="bottom-right" richColors closeButton className="app-toaster" />
    </div>
  );
}

function Title({
  eyebrow,
  children,
  note,
}: {
  eyebrow: string;
  children: ReactNode;
  note: string;
}) {
  return (
    <div className="title">
      <span>{eyebrow}</span>
      <h1>{children}</h1>
      <p>{note}</p>
    </div>
  );
}
function ChevronIcon({ className = "" }: { className?: string }) {
  return <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false"><path d="m6 9 6 6 6-6" /></svg>;
}
function CloseIcon() {
  return <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true" focusable="false"><path d="m6 6 12 12M18 6 6 18" /></svg>;
}
function Empty({ title = "Пока пусто", children, action, className = "" }: { title?: string; children: ReactNode; action?: ReactNode; className?: string }) {
  return (
    <div className={`empty ${className}`}>
      <b>{title}</b>
      <p>{children}</p>
      {action}
    </div>
  );
}
function Notice({ children, tone = "neutral", role }: { children: ReactNode; tone?: "neutral" | "success" | "warning" | "danger" | "info"; role?: "status" | "alert" }) {
  return <p className={`notice notice-${tone}`} role={role ?? (tone === "danger" ? "alert" : "status")}>{children}</p>;
}
function Status({ value }: { value: string }) {
  const meta = STATUS_META[value];
  return (
    <span className={`status status-${meta?.tone ?? "neutral"} s-${value.toLowerCase()}`} data-status={value}>
      {humanStatus(value)}
    </span>
  );
}

type OverviewIconName = "arrow" | "check" | "scan" | "fileCheck" | "sliders";
function OverviewIcon({ name }: { name: OverviewIconName }) {
  const paths: Record<OverviewIconName, string> = {
    arrow: "M5 12h14m-6-6 6 6-6 6",
    check: "m5 12 4 4L19 6",
    scan: "M3 7V5a2 2 0 0 1 2-2h2m10 0h2a2 2 0 0 1 2 2v2m0 10v2a2 2 0 0 1-2 2h-2M7 21H5a2 2 0 0 1-2-2v-2m6-5a3 3 0 1 0 6 0 3 3 0 0 0-6 0m5 3 3 3",
    fileCheck: "M6 3h9l3 3v15H6zM9 14l2 2 4-5",
    sliders: "M4 6h16M4 12h16M4 18h16M8 4v4m8 2v4m-5 4v4",
  };
  return <svg aria-hidden="true" focusable="false" viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d={paths[name]} /></svg>;
}

function Dashboard() {
  const profiles = useQuery({
    queryKey: ["profiles"],
    queryFn: () => api<Profile[]>("/profiles"),
  });
  const dashboardProfile = profiles.data?.[0];
  const dashboardResumes = useQuery({
    queryKey: ["dashboard-resumes", dashboardProfile?.id],
    queryFn: () => api<Resume[]>(`/profiles/${dashboardProfile?.id}/resumes`),
    enabled: Boolean(dashboardProfile),
  });
  const sessions = useQuery({
    queryKey: ["sessions"],
    queryFn: () => api<JobSession[]>("/sessions"),
  });
  const modelStatus = useQuery({
    queryKey: ["model-status"],
    queryFn: () => api<{ connected: boolean; model_available: boolean; model: string }>("/model/status"),
    retry: false,
  });
  const active = sessions.data?.[0];
  const hasProfile = Boolean(profiles.data?.length);
  const hasResume = Boolean(dashboardResumes.data?.some((resume) => resume.selected_for_matching));
  const activeStatus = active ? humanStatus(active.status) : "Нет сессии";
  const primaryHref = hasResume ? "/session" : "/profile";
  const primaryLabel = hasResume ? "Запустить сессию" : "Настроить профиль";
  const secondaryHref = "/model";
  const secondaryLabel = "Добавить API модели";
  const sessionReady = Boolean(active && ["RUNNING", "EVALUATING", "CREATED"].includes(active.status));
  return <div className="overview-page">
    <section className="overview-hero-section" aria-labelledby="overview-title">
      <div className="overview-container overview-hero">
        <div className="overview-copy">
          <span className="overview-eyebrow">JOB ORCHESTRATOR · ЦЕНТР ПОИСКА</span>
          <h1 id="overview-title">Меньше шума.<br />Больше <em>подходящих</em> вакансий.</h1>
          <p>Соберите профиль один раз, а затем поручите ИИ найти и разобрать вакансии по заданным критериям и лимитам. Вы наблюдаете за процессом и управляете условиями поиска.</p>
          <div className="overview-actions">
            <NavLink className="button-link overview-primary overview-quiet-control" to={primaryHref}>{primaryLabel} <OverviewIcon name="arrow" /></NavLink>
            <NavLink className="overview-text-link overview-quiet-link" to={secondaryHref}>{secondaryLabel}</NavLink>
          </div>
          <div className="overview-status-strip" aria-label="Готовность к поиску">
            <span className={modelStatus.data?.connected && modelStatus.data.model_available ? "is-done" : ""}><OverviewIcon name="check" />API модели {modelStatus.data?.connected && modelStatus.data.model_available ? "добавлен" : "не добавлен"}</span>
            <span className={hasProfile ? "is-done" : ""}><OverviewIcon name="check" />Профиль {hasProfile ? "заполнен" : "не настроен"}</span>
            <span className={hasResume ? "is-done" : ""}><OverviewIcon name="check" />Резюме {hasResume ? "выбрано" : "не выбрано"}</span>
            <span><i key={`status-pulse-strip-${active?.status ?? "none"}`} className={sessionReady ? "overview-status-pulse" : ""} />Сессия {activeStatus.toLowerCase()}</span>
          </div>
        </div>
        <div className="overview-preview" aria-label="Статус рабочего процесса">
          <div className="preview-top"><span key={`status-pulse-preview-${active?.status ?? "none"}`} className={`preview-dot${sessionReady ? " overview-status-pulse" : ""}`} />Рабочий процесс <span className="preview-live">{activeStatus}</span></div>
          <div className="preview-job"><span className="preview-logo">J</span><div><strong>Подходящие вакансии</strong><small>Оценка по профилю и условиям сессии</small></div><b>{sessionReady ? "Оценивает" : active ? activeStatus : hasResume ? "Готово к запуску" : "Нужно настроить"}</b></div>
          <div className="preview-lines">
            <div className="preview-line"><i className={hasProfile ? "is-done" : ""}>{hasProfile ? "✓" : "1"}</i><span>Личный профиль</span><small>{hasProfile ? "Заполнен" : "Нужно настроить"}</small></div>
            <div className="preview-line"><i className={hasResume ? "is-done" : ""}>{hasResume ? "✓" : "2"}</i><span>Выбранное резюме</span><small>{hasResume ? "Готово к оценке" : "Выберите резюме"}</small></div>
            <div className="preview-line"><i className={sessionReady ? "is-done" : ""}>{sessionReady ? "✓" : "3"}</i><span>Наблюдение за сессией</span><small>{active ? activeStatus : "Настройте критерии и лимиты"}</small></div>
          </div>
          <div className="preview-proof">
            <div className="preview-proof-head"><div><span>Вакансия на проверке</span><h2>Продуктовая роль</h2><p>Сопоставление с выбранным резюме</p></div><b>Разбор</b></div>
            <div className="preview-proof-list">
              <div><OverviewIcon name="fileCheck" /><p><strong>Опыт и задачи</strong><span>ИИ ищет подтверждение требований в опыте кандидата.</span></p></div>
              <div><OverviewIcon name="sliders" /><p><strong>Критерии сессии</strong><span>Формат, роль и ограничения учитываются при оценке.</span></p></div>
              <div><OverviewIcon name="scan" /><p><strong>Результат для просмотра</strong><span>Причины соответствия остаются видимыми пользователю.</span></p></div>
            </div>
          </div>
        </div>
      </div>
    </section>
  </div>;
}

const blankPersonal: Omit<PersonalProfileData, "resumes"> = {
  full_name: "",
  residence: "",
  job_search_locations: [],
  contacts: { phone: "", email: "", messengers: [] },
  education: [],
  languages: [],
  driver_license: false,
};

const employmentOptions: Array<[EmploymentType, string]> = [
  ["permanent", "Постоянная работа"],
  ["internship", "Стажировка"],
  ["part_time", "Подработка"],
  ["volunteering", "Волонтёрство"],
];
const formatOptions: Array<[string, string]> = [
  ["hybrid", "Гибрид"],
  ["remote", "Удалённо"],
  ["office", "Офис"],
  ["traveling", "Разъездная"],
  ["shift", "Вахта"],
];
const educationOptions: Array<[EducationType, string]> = [
  ["higher", "Высшее"],
  ["secondary_vocational", "Среднее специальное"],
  ["school", "Школьное"],
];

function SingleSelect({ options, value, onValueChange, label, placeholder, className, disabled }: { options: ReadonlyArray<readonly [string, string]>; value: string; onValueChange: (value: string) => void; label: string; placeholder?: string; className?: string; disabled?: boolean }) {
  return <div className={`single-select ${className ?? ""}`}>
    <span className="single-select-label">{label}</span>
    <Select.Root items={options.map(([optionValue, optionLabel]) => ({ value: optionValue, label: optionLabel }))} value={value || null} onValueChange={(next) => onValueChange(next ?? "")} disabled={disabled}>
      <Select.Trigger className="single-select-trigger" aria-label={label}>
        <Select.Value className="single-select-value" placeholder={placeholder} />
        <ChevronIcon className="single-select-chevron" />
      </Select.Trigger>
      <Select.Portal><Select.Positioner className="single-select-positioner" alignItemWithTrigger={false} sideOffset={6}><Select.Popup className="single-select-popup"><Select.List className="single-select-list">
        {options.map(([optionValue, optionLabel]) => <Select.Item className="single-select-option" key={optionValue} value={optionValue}><Select.ItemText className="single-select-option-copy">{optionLabel}</Select.ItemText><Select.ItemIndicator className="single-select-indicator">✓</Select.ItemIndicator></Select.Item>)}
      </Select.List></Select.Popup></Select.Positioner></Select.Portal>
    </Select.Root>
  </div>;
}

function newEducation(type: EducationType = "higher"): Education {
  return { type, institution: "", faculty: "", specialty: "", start_date: "", end_date: "", degree: null };
}
function newExperience(): WorkExperience {
  return { company: "", position: "", start_date: "", end_date: "", duties: "" };
}
function newResume(profileId: number): Resume {
  return {
    id: 0,
    profile_id: profileId,
    name: "Новое резюме",
    desired_title: "",
    desired_salary: "",
    employment_types: [],
    work_formats: [],
    business_trips: null,
    experiences: [],
    skills: [],
    about: "",
    selected_for_matching: false,
  };
}

function PersonalEditor({
  value,
  onChange,
  onSave,
  saving,
}: {
  value: Omit<PersonalProfileData, "resumes">;
  onChange: (value: Omit<PersonalProfileData, "resumes">) => void;
  onSave: () => void;
  saving: boolean;
}) {
  const change = <K extends keyof Omit<PersonalProfileData, "resumes">>(key: K, next: Omit<PersonalProfileData, "resumes">[K]) =>
    onChange({ ...value, [key]: next });
  const changeEducation = (index: number, next: Partial<Education>) => {
    const education = value.education.map((item, itemIndex) => itemIndex === index ? { ...item, ...next } : item);
    change("education", education);
  };
  const changeLanguage = (index: number, next: Partial<Language>) => {
    const languages = value.languages.map((item, itemIndex) => itemIndex === index ? { ...item, ...next } : item);
    change("languages", languages);
  };
  return (
    <article className="panel form profile-personal">
      <div className="panelhead">
        <div><span className="eyebrow">БЛОК 1</span><h2>Личная информация</h2></div>
        <span className="profile-progress">Профиль кандидата</span>
      </div>
      <div className="form-content">
      <div className="profile-pair-row">
        <label className="profile-field">ФИО<input value={value.full_name ?? ""} onChange={(e) => change("full_name", e.target.value)} /></label>
        <label className="profile-field">Место проживания<input value={value.residence ?? ""} onChange={(e) => change("residence", e.target.value)} /></label>
      </div>
      <label className="profile-full-field">Где ищу работу<input value={value.job_search_locations.join(", ")} onChange={(e) => change("job_search_locations", e.target.value.split(",").map((item) => item.trim()).filter(Boolean))} placeholder="Города или направления через запятую" /></label>
      <div className="subsection form-rail">
        <div className="section-heading"><div><h3>Контакты</h3><small>Телефон, email и список мессенджеров.</small></div><button type="button" className="secondary" onClick={() => change("contacts", { ...value.contacts, messengers: [...value.contacts.messengers, ""] })}>+ Мессенджер</button></div>
        <div className="repeat-list">
          <div className="profile-pair-row"><label className="profile-field">Телефон<input aria-label="Телефон" value={value.contacts.phone ?? ""} onChange={(e) => change("contacts", { ...value.contacts, phone: e.target.value })} /></label><label className="profile-field">Email<input aria-label="Email" value={value.contacts.email ?? ""} onChange={(e) => change("contacts", { ...value.contacts, email: e.target.value })} /></label></div>
          {value.contacts.messengers.map((messenger, index) => <div className="profile-remove-row animated-repeat-item" key={index}><input className="profile-field" aria-label={`Мессенджер ${index + 1}`} value={messenger} onChange={(e) => change("contacts", { ...value.contacts, messengers: value.contacts.messengers.map((item, itemIndex) => itemIndex === index ? e.target.value : item) })} placeholder="Telegram, WhatsApp или другой мессенджер" /><button type="button" className="icon-button" aria-label={`Удалить мессенджер ${index + 1}`} onClick={(event) => delayedRemove(event, (currentIndex) => change("contacts", { ...value.contacts, messengers: value.contacts.messengers.filter((_, itemIndex) => itemIndex !== currentIndex) }))}><CloseIcon /></button></div>)}
        </div>
      </div>
      <div className="subsection form-rail">
        <div className="section-heading"><div><h3>Образование</h3><small>Выберите тип для каждого учебного заведения.</small></div><button type="button" className="secondary" onClick={() => change("education", [...value.education, newEducation()])}>+ Образование</button></div>
        <div className="repeat-list">
          {value.education.map((item, index) => <div className="nested-card education-card animated-repeat-item" key={index}>
            <div className="profile-remove-row profile-education-header"><SingleSelect label={`Тип образования ${index + 1}`} options={educationOptions} value={item.type} onValueChange={(next) => changeEducation(index, { type: next as EducationType })} /><button type="button" className="icon-button" aria-label={`Удалить образование ${index + 1}`} onClick={(event) => delayedRemove(event, (currentIndex) => change("education", value.education.filter((_, itemIndex) => itemIndex !== currentIndex)))}><CloseIcon /></button></div>
            <div className={item.type === "school" ? "profile-full-field" : "profile-pair-row"}>
              <label className="profile-field">{item.type === "higher" ? "Университет" : "Учебное заведение"}<input value={item.institution} onChange={(e) => changeEducation(index, { institution: e.target.value })} /></label>
              {item.type !== "school" && <label className="profile-field">Факультет<input value={item.faculty ?? ""} onChange={(e) => changeEducation(index, { faculty: e.target.value })} /></label>}
            </div>
            {item.type === "higher" && <SingleSelect label="Степень" placeholder="Выберите степень" options={[["bachelor", "Бакалавр"], ["master", "Магистр"], ["specialist", "Специалист"], ["postgraduate", "Аспирант"]]} value={item.degree ?? ""} onValueChange={(next) => changeEducation(index, { degree: (next || null) as Degree | null })} />}
            {item.type !== "school" && <label className="profile-full-field">Специальность<input value={item.specialty ?? ""} onChange={(e) => changeEducation(index, { specialty: e.target.value })} /></label>}
            <div className="profile-pair-row"><label className="profile-field">Дата начала обучения<input type="month" value={item.start_date ?? ""} onChange={(e) => changeEducation(index, { start_date: e.target.value || null })} /></label><label className="profile-field">Дата окончания обучения<input type="month" value={item.end_date ?? ""} onChange={(e) => changeEducation(index, { end_date: e.target.value || null })} /></label></div>
          </div>)}
        </div>
      </div>
      <div className="subsection form-rail">
        <div className="section-heading"><div><h3>Языки</h3><small>Язык и уровень владения.</small></div><button type="button" className="secondary" onClick={() => change("languages", [...value.languages, { language: "", proficiency: "" }])}>+ Язык</button></div>
          <div className="repeat-list">{value.languages.map((language, index) => <div className="profile-pair-remove-row animated-repeat-item" key={index}><input className="profile-field" aria-label={`Язык ${index + 1}`} value={language.language} onChange={(e) => changeLanguage(index, { language: e.target.value })} placeholder="Например, английский" /><input className="profile-field" aria-label={`Уровень языка ${index + 1}`} value={language.proficiency} onChange={(e) => changeLanguage(index, { proficiency: e.target.value })} placeholder="Например, B2" /><button type="button" className="icon-button" aria-label={`Удалить язык ${index + 1}`} onClick={(event) => delayedRemove(event, (currentIndex) => change("languages", value.languages.filter((_, itemIndex) => itemIndex !== currentIndex)))}><CloseIcon /></button></div>)}</div>
      </div>
      <label className="checkline"><input type="checkbox" checked={value.driver_license ?? false} onChange={(e) => change("driver_license", e.target.checked)} /> Есть водительские права</label>
      <div className="actions sticky-actions profile-save-bar"><button type="button" className="primary" onClick={onSave} disabled={saving}>{saving ? "Сохраняю…" : "Сохранить личный профиль"}</button></div>
      </div>
    </article>
  );
}

function MultiSelect({
  label,
  hint,
  options,
  values,
  onToggle,
}: {
  label: string;
  hint?: string;
  options: ReadonlyArray<readonly [string, string]>;
  values: string[];
  onToggle: (value: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const listId = useId();
  const selectedLabels = options.filter(([value]) => values.includes(value)).map(([, optionLabel]) => optionLabel);
  return <Popover.Root open={open} onOpenChange={setOpen}>
  <div className="multi-select field-wide">
    <Popover.Trigger render={<button type="button" className="multi-select-trigger" aria-label={label} aria-controls={listId} />}>
      <span className="multi-select-trigger-copy"><span className="multi-select-label">{label}</span><span className="multi-select-value">{selectedLabels.join(", ") || "Выберите варианты"}</span></span>
      <ChevronIcon className={`multi-select-chevron${open ? " is-open" : ""}`} />
    </Popover.Trigger>
    {hint && <small className="multi-select-hint">{hint}</small>}
    <Popover.Portal><Popover.Positioner className="multi-select-positioner"><Popover.Popup className="multi-select-popover" id={listId}>
      <div className="multi-select-options">
        {options.map(([value, optionLabel]) => <label className={`multi-select-option${values.includes(value) ? " is-selected" : ""}`} key={value}>
          <input type="checkbox" checked={values.includes(value)} onChange={() => onToggle(value)} />
          <span>{optionLabel}</span>
        </label>)}
      </div>
      <button type="button" className="multi-select-confirm" onClick={() => setOpen(false)}>Выбрать</button>
    </Popover.Popup></Popover.Positioner></Popover.Portal>
  </div></Popover.Root>;
}

export function Notifications() {
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const knownIds = useRef<Set<number> | null>(null);
  const query = useQuery({ queryKey: ["notifications"], queryFn: async () => { const value = await api<unknown>("/notifications"); return Array.isArray(value) ? value as Notification[] : []; }, refetchInterval: import.meta.env.MODE === "test" ? false : 2500, refetchIntervalInBackground: false, retry: false });
  // Keep the client ordering identical to the API ordering.  The id tie-breaker
  // is important when SQLite timestamps have the same precision (and also keeps
  // malformed/missing timestamps from making the order unstable).
  const notifications = [...(Array.isArray(query.data) ? query.data : [])].sort((a, b) => {
    const byDate = (Date.parse(b.created_at) || 0) - (Date.parse(a.created_at) || 0);
    return byDate || b.id - a.id;
  });
  const unread = notifications.filter((item) => !item.read_at).length;
  useEffect(() => {
    if (!query.data) return;
    const ids = new Set(query.data.map((item) => item.id));
    if (knownIds.current && [...ids].some((id) => !knownIds.current!.has(id))) {
      try {
        const AudioContextClass = window.AudioContext ?? (window as typeof window & { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
        if (AudioContextClass) {
          const context = new AudioContextClass();
          const oscillator = context.createOscillator();
          const gain = context.createGain();
          oscillator.frequency.value = 660; gain.gain.setValueAtTime(0.04, context.currentTime);
          gain.gain.exponentialRampToValueAtTime(0.001, context.currentTime + 0.12);
          oscillator.connect(gain); gain.connect(context.destination); oscillator.onended = () => { void context.close().catch(() => undefined); }; oscillator.start(); oscillator.stop(context.currentTime + 0.12);
        }
      } catch { /* autoplay and unavailable AudioContext are harmless */ }
    }
    knownIds.current = ids;
  }, [query.data]);
  const markRead = async (item: Notification) => {
    try {
      if (!item.read_at) await api(`/notifications/${item.id}/read`, { method: "PATCH" });
    } finally {
      setOpen(false); navigate(item.target_path || "/session");
      if (!item.read_at) await query.refetch().catch(() => undefined);
    }
  };
  const readAll = async () => { if (unread) { await api("/notifications/read-all", { method: "POST" }); await query.refetch(); } };
  return <Popover.Root open={open} onOpenChange={setOpen}><div className="notifications">
    <Popover.Trigger render={<button type="button" className="notification-trigger" aria-label="Уведомления" />}>
      <svg className="notification-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">
        <path d="M18 9a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9" />
        <path d="M10 21h4" />
      </svg>{unread > 0 && <i role="status" className="notification-badge" aria-label={`${unread} непрочитанных`} />}
    </Popover.Trigger>
    <Popover.Portal><Popover.Positioner className="notification-positioner"><Popover.Popup className="notification-popover" role="dialog" aria-label="Уведомления">
      <div className="notification-head"><strong>Уведомления</strong><button type="button" onClick={() => void readAll()} disabled={!unread}>Прочитать все</button></div>
      {query.isLoading ? <p className="notification-empty">Загрузка…</p> : notifications.length === 0 ? <p className="notification-empty">Новых уведомлений нет</p> : <div className="notification-list">{notifications.map((item) => <button type="button" className={`notification-item${item.read_at ? "" : " unread"}`} key={item.id} onClick={() => void markRead(item)}><strong>{item.title}</strong><span>{item.message}</span><time dateTime={item.created_at}>{new Date(item.created_at).toLocaleString("ru-RU")}</time></button>)}</div>}
    </Popover.Popup></Popover.Positioner></Popover.Portal>
  </div></Popover.Root>;
}
function delayedRemove(event: React.MouseEvent<HTMLButtonElement>, remove: (currentIndex: number) => void) {
  const button = event.currentTarget;
  const item = button.closest<HTMLElement>(".animated-repeat-item");
  if (!item || item.dataset.removing === "true") return;
  const getCurrentIndex = () => {
    const parent = item.parentElement;
    return parent ? Array.from(parent.children).filter((child) => child.classList.contains("animated-repeat-item")).indexOf(item) : -1;
  };
  if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) { remove(getCurrentIndex()); return; }
  item.dataset.removing = "true"; item.classList.add("is-removing"); button.disabled = true;
  window.setTimeout(() => remove(getCurrentIndex()), 120);
}

function ResumeEditor({ value, onSave, onCancel, saving }: { value: Resume; onSave: (value: Resume) => void; onCancel: () => void; saving: boolean }) {
  const [draft, setDraft] = useState(value);
  const [skillsText, setSkillsText] = useState(value.skills.join(", "));
  useEffect(() => { setDraft(value); setSkillsText(value.skills.join(", ")); }, [value]);
  const wordCount = draft.about.trim() ? draft.about.trim().split(/\s+/u).length : 0;
  const update = (next: Partial<Resume>) => setDraft((current) => ({ ...current, ...next }));
  const toggleEmployment = (type: EmploymentType) => update({ employment_types: draft.employment_types.includes(type) ? draft.employment_types.filter((item) => item !== type) : [...draft.employment_types, type] });
  const updateExperience = (index: number, next: Partial<WorkExperience>) => update({ experiences: draft.experiences.map((item, itemIndex) => itemIndex === index ? { ...item, ...next } : item) });
  return <article className="panel form resume-editor">
    <div className="panelhead"><div><span className="eyebrow">БЛОК 2</span><h2>{draft.id ? "Редактирование резюме" : "Новое резюме"}</h2></div><button type="button" className="icon-button" onClick={onCancel} aria-label="Закрыть редактор резюме"><CloseIcon /></button></div>
    <div className="form-content">
    <label className="field-medium">Название резюме<input value={draft.name} onChange={(e) => update({ name: e.target.value })} placeholder="Например, Product Manager" /></label>
    <div className="row form-row"><label className="field-medium">Предполагаемая должность<input value={draft.desired_title ?? ""} onChange={(e) => update({ desired_title: e.target.value })} /></label><label className="field-compact">Желаемый доход<input value={draft.desired_salary ?? ""} onChange={(e) => update({ desired_salary: e.target.value })} placeholder="Например, 180 000 ₽" /></label></div>
    <MultiSelect label="Желаемый тип занятости" options={employmentOptions} values={draft.employment_types} onToggle={(type) => toggleEmployment(type as EmploymentType)} />
    <MultiSelect label="Формат работы" hint="Можно выбрать несколько вариантов." options={formatOptions} values={draft.work_formats} onToggle={(format) => update({ work_formats: draft.work_formats.includes(format) ? draft.work_formats.filter((item) => item !== format) : [...draft.work_formats, format] })} />
    <SingleSelect className="field-compact" label="Командировки" placeholder="Выберите вариант" options={[["can", "Могу"], ["cannot", "Не могу"]]} value={draft.business_trips === null || draft.business_trips === undefined ? "" : draft.business_trips ? "can" : "cannot"} onValueChange={(next) => update({ business_trips: next === "" ? null : next === "can" })} />
    <div className="subsection form-rail"><div className="section-heading"><div><h3>Опыт работы</h3><small>Добавьте должности и обязанности.</small></div><button type="button" className="secondary" onClick={() => update({ experiences: [...draft.experiences, newExperience()] })}>+ Опыт</button></div><div className="repeat-list">{draft.experiences.map((experience, index) => <div className="nested-card experience-card animated-repeat-item" key={index}><div className="repeat-row"><strong>Опыт #{index + 1}</strong><button type="button" className="icon-button" aria-label={`Удалить опыт ${index + 1}`} onClick={(event) => delayedRemove(event, (currentIndex) => update({ experiences: draft.experiences.filter((_, itemIndex) => itemIndex !== currentIndex) }))}><CloseIcon /></button></div><div className="row form-row"><label className="field-medium">Компания<input value={experience.company} onChange={(e) => updateExperience(index, { company: e.target.value })} /></label><label className="field-medium">Должность<input value={experience.position} onChange={(e) => updateExperience(index, { position: e.target.value })} /></label></div><div className="row form-row"><label className="field-compact">Начало работы<input type="month" value={experience.start_date ?? ""} onChange={(e) => updateExperience(index, { start_date: e.target.value || null })} /></label><label className="field-compact">Конец работы<input type="month" value={experience.end_date ?? ""} onChange={(e) => updateExperience(index, { end_date: e.target.value || null })} placeholder="Оставьте пустым, если работаете сейчас" /></label></div><label className="field-prose">Описание обязанностей<textarea className="experience-duties" aria-label={`Описание обязанностей ${index + 1}`} value={experience.duties} onChange={(e) => updateExperience(index, { duties: e.target.value })} /></label></div>)}</div></div>
    <label className="field-wide">Навыки<small>Введите навыки через запятую — каждый станет отдельным тегом.</small><input value={skillsText} onChange={(e) => setSkillsText(e.target.value)} placeholder="CustDev, Scrum, аналитика" /></label>
    <div className="tag-list field-wide" aria-label="Навыки">{skillsText.split(",").map((skill) => skill.trim()).filter(Boolean).map((skill) => <span className="tag" key={skill}>{skill}</span>)}</div>
    <label className="field-prose">О себе<textarea className="about-text" value={draft.about} onChange={(e) => { const words = e.target.value.trim() ? e.target.value.trim().split(/\s+/u) : []; if (words.length <= 500) update({ about: e.target.value }); }} /><small className={wordCount > 500 ? "word-limit" : ""}>{wordCount} / 500 слов</small></label>
    <div className="actions form-rail sticky-actions"><button type="button" className="primary" onClick={() => onSave({ ...draft, skills: skillsText.split(",").map((skill) => skill.trim()).filter(Boolean) })} disabled={saving}>{saving ? "Сохраняю…" : "Сохранить резюме"}</button><button type="button" className="secondary" onClick={onCancel}>Отмена</button></div>
    </div>
  </article>;
}

function ProfilePage() {
  const profiles = useQuery({ queryKey: ["profiles"], queryFn: () => api<Profile[]>("/profiles") });
  const current = profiles.data?.[0];
  const resumes = useQuery({ queryKey: ["resumes", current?.id], queryFn: () => api<Resume[]>(`/profiles/${current?.id}/resumes`), enabled: Boolean(current) });
  const qc = useQueryClient();
  const [personal, setPersonal] = useState<Omit<PersonalProfileData, "resumes">>(blankPersonal);
  const [editingResume, setEditingResume] = useState<Resume | null>(null);
  const [message, setMessage] = useState("");
  const [messageTone, setMessageTone] = useState<"neutral" | "success" | "warning" | "danger" | "info">("neutral");
  useEffect(() => { if (current) { const data = { ...current.data }; delete data.resumes; setPersonal({ ...blankPersonal, ...data, contacts: { ...blankPersonal.contacts, ...data.contacts, messengers: data.contacts?.messengers ?? [] }, job_search_locations: data.job_search_locations ?? [], education: data.education ?? [], languages: data.languages ?? [] }); } }, [current]);
  const personalPayload = (value: Omit<PersonalProfileData, "resumes">) => ({
    full_name: value.full_name || null,
    residence: value.residence || null,
    job_search_locations: value.job_search_locations,
    contacts: { phone: value.contacts.phone || null, email: value.contacts.email || null, messengers: value.contacts.messengers.filter(Boolean) },
    education: value.education.map((item) => ({
      type: item.type,
      institution: item.institution,
      start_date: item.start_date || null,
      end_date: item.end_date || null,
      ...(item.type !== "school" ? { faculty: item.faculty || null, specialty: item.specialty || null } : {}),
      ...(item.type === "higher" ? { degree: item.degree || null } : {}),
    })),
    languages: value.languages.map((item) => ({ language: item.language, proficiency: item.proficiency })),
    driver_license: value.driver_license ?? null,
  });
  const resumePayload = (resume: Resume) => ({ name: resume.name, desired_title: resume.desired_title || null, desired_salary: resume.desired_salary || null, employment_types: resume.employment_types, work_formats: resume.work_formats, business_trips: resume.business_trips ?? null, experiences: resume.experiences.map(({ company, position, start_date, end_date, duties }) => ({ company, position, start_date: start_date || null, end_date: end_date || null, duties })), skills: resume.skills, about: resume.about, selected_for_matching: resume.selected_for_matching });
  const savePersonal = useMutation({ mutationFn: () => current ? api<Profile>(`/profiles/${current.id}`, { method: "PATCH", body: JSON.stringify(personalPayload(personal)) }) : api<Profile>("/profiles", { method: "POST", body: JSON.stringify(personalPayload(personal)) }), onSuccess: async () => { setMessageTone("success"); setMessage("Личный профиль сохранён."); toast.success("Личный профиль сохранён"); await qc.invalidateQueries({ queryKey: ["profiles"] }); }, onError: (e) => { setMessageTone("danger"); setMessage(e.message); toast.error(e.message); } });
  const saveResume = useMutation({ mutationFn: (resume: Resume) => current ? resume.id ? api<Resume>(`/profiles/${current.id}/resumes/${resume.id}`, { method: "PATCH", body: JSON.stringify(resumePayload(resume)) }) : api<Resume>(`/profiles/${current.id}/resumes`, { method: "POST", body: JSON.stringify(resumePayload(resume)) }) : Promise.reject(new Error("Сначала создайте профиль.")), onSuccess: async () => { setMessageTone("success"); setEditingResume(null); setMessage("Резюме сохранено."); toast.success("Резюме сохранено"); await qc.invalidateQueries({ queryKey: ["resumes", current?.id] }); }, onError: (e) => { setMessageTone("danger"); setMessage(e.message); toast.error(e.message); } });
  const removeResume = useMutation({ mutationFn: (id: number) => current ? api(`/profiles/${current.id}/resumes/${id}`, { method: "DELETE" }) : Promise.reject(new Error("Профиль не найден.")), onSuccess: async () => { setMessageTone("success"); setMessage("Резюме удалено."); toast.success("Резюме удалено"); await qc.invalidateQueries({ queryKey: ["resumes", current?.id] }); }, onError: (e) => { setMessageTone("danger"); setMessage(e.message); toast.error(e.message); } });
  const upload = useMutation({ mutationFn: async (file: File) => { if (!current) throw new Error("Сначала сохраните личный профиль."); const body = new FormData(); body.append("file", file); return api<{ profile: Profile; resume: Resume }>(`/profiles/${current.id}/resumes/import`, { method: "POST", body }); }, onMutate: (file) => { setMessageTone("info"); setMessage(`Файл «${file.name}» принят. Заполняем профиль и отдельное резюме…`); }, onSuccess: async (result) => { qc.setQueryData<Profile[]>(["profiles"], (items = []) => [result.profile, ...items.filter((item) => item.id !== result.profile.id)]); qc.setQueryData<Resume[]>(["resumes", result.profile.id], (items = []) => [result.resume, ...items.filter((item) => item.id !== result.resume.id)]); setMessageTone("success"); setMessage(`Импорт «${result.resume.name || result.resume.original_filename || "резюме"}» завершён. Проверьте и отредактируйте поля.`); toast.success("Импорт резюме завершён"); await qc.invalidateQueries({ queryKey: ["profiles"] }); }, onError: (e) => { setMessageTone("danger"); setMessage(e.message); toast.error(e.message); } });
  const toggleSelected = async (resume: Resume) => { if (!current) return; try { await api<Resume>(`/profiles/${current.id}/resumes/${resume.id}`, { method: "PATCH", body: JSON.stringify(resumePayload({ ...resume, selected_for_matching: !resume.selected_for_matching })) }); await qc.invalidateQueries({ queryKey: ["resumes", current.id] }); } catch (error) { setMessageTone("danger"); setMessage(error instanceof Error ? error.message : "Не удалось изменить выбор резюме"); } };
  return <section className="page">
    <Title eyebrow="ПРОФИЛЬ КАНДИДАТА" note="Заполните личные данные один раз, а затем создавайте отдельные резюме под разные направления поиска. Только выбранные резюме попадут в оценку вакансий.">Профиль и резюме под вашим контролем</Title>
    {!current && <Empty title="Создайте личный профиль" className="panel empty-profile" action={<button type="button" className="primary" onClick={() => savePersonal.mutate()} disabled={savePersonal.isPending}>{savePersonal.isPending ? "Создаём…" : "Создать профиль"}</button>}>Начните с личной информации. После сохранения можно импортировать PDF, DOCX или TXT и отредактировать результат.</Empty>}
    {current && <>
      <label className={`upload ${upload.isPending ? "busy" : ""}`}><input type="file" accept=".pdf,.docx,.txt" aria-label="Импортировать резюме" disabled={upload.isPending} onChange={(event) => { const file = event.currentTarget.files?.[0]; event.currentTarget.value = ""; if (file) upload.mutate(file); }} /><span>{upload.isPending ? `Обрабатываем «${upload.variables?.name}»…` : "Импортировать PDF, DOCX или TXT"}</span><small>Парсер заполнит личный профиль и создаст отдельное резюме. После импорта проверьте поля.</small></label>
      {message && <Notice tone={messageTone}>{message}</Notice>}
      <PersonalEditor value={personal} onChange={setPersonal} onSave={() => savePersonal.mutate()} saving={savePersonal.isPending} />
      <section className="resume-section"><div className="section-heading resume-section-heading"><div><span className="eyebrow">БЛОК 2</span><h2>Мои резюме</h2><p>Отметьте одно или несколько резюме, которые передавать ИИ при оценке релевантности вакансий.</p></div><button type="button" className="primary" onClick={() => setEditingResume(newResume(current.id))}>+ Создать резюме</button></div>
        {editingResume && <ResumeEditor value={editingResume} onSave={(value) => saveResume.mutate(value)} onCancel={() => setEditingResume(null)} saving={saveResume.isPending} />}
        {resumes.data?.length ? <div className="resume-grid">{resumes.data.map((resume) => <article className={`panel resume-card ${resume.selected_for_matching ? "selected" : ""}`} key={resume.id}><div className="resume-card-top"><div><span className="eyebrow">РЕЗЮМЕ</span><h3>{resume.name || resume.desired_title || "Без названия"}</h3></div><label className="selection-control"><input type="checkbox" checked={resume.selected_for_matching} onChange={() => void toggleSelected(resume)} /> Передавать модели</label></div><div className="resume-meta"><span>{resume.desired_title || "Должность не указана"}</span><span>{resume.skills?.length ?? 0} навыков</span><span>{resume.experiences?.length ?? 0} мест опыта</span></div><p>{resume.about || "Добавьте короткое описание о себе как о работнике."}</p><div className="tag-list">{resume.skills?.slice(0, 8).map((skill) => <span className="tag" key={skill}>{skill}</span>)}</div><div className="actions"><button type="button" className="secondary" onClick={() => setEditingResume(resume)}>Редактировать</button><button type="button" className="danger" onClick={() => { if (window.confirm("Удалить это резюме?")) removeResume.mutate(resume.id); }}>Удалить</button></div></article>)}</div> : <Empty title="Резюме пока нет">Создайте резюме вручную или импортируйте файл сверху.</Empty>}
      </section>
    </>}
  </section>;
}

function SessionPage() {
  const [guaranteedApplication, setGuaranteedApplication] = useState(false);
  const qc = useQueryClient();
  const profiles = useQuery({
    queryKey: ["profiles"],
    queryFn: () => api<Profile[]>("/profiles"),
  });
  const sessionProfile = profiles.data?.[0];
  const adapters = useQuery({ queryKey: ["adapters"], queryFn: async () => { const value = await api<Adapter[] | unknown>("/adapters"); return Array.isArray(value) ? value as Adapter[] : []; } });
  const sessionResumes = useQuery({
    queryKey: ["session-resumes", sessionProfile?.id],
    queryFn: () => api<Resume[]>(`/profiles/${sessionProfile?.id}/resumes`),
    enabled: Boolean(sessionProfile),
  });
  const sessions = useQuery({
    queryKey: ["sessions"],
    queryFn: () => api<JobSession[]>("/sessions"),
    refetchInterval: 2000,
  });
  const terminalStatuses = ["COMPLETED", "STOPPED", "FAILED"];
  const profileReady = Boolean(
    sessionProfile &&
      sessionResumes.data?.some((resume) => resume.selected_for_matching),
  );
  const { draft, updateDraft, status: draftStatus, conflict: draftConflict, loadSaved } = useSessionDraft();
  const { adapter, applicationLimit, desiredJobDescription, unlimitedApplications, influence } = draft;
  const blockedByAdapter = (sessions.data ?? []).some((session) => session.adapter_id === adapter && !terminalStatuses.includes(session.status));
  const [message, setMessage] = useState("");
  const [messageTone, setMessageTone] = useState<"neutral" | "success" | "warning" | "danger" | "info">("neutral");
  const validLimit = (value: string, unlimited: boolean) =>
    unlimited || /^[1-9]\d*$/.test(value);
  const limitsAreValid = validLimit(applicationLimit, unlimitedApplications);
  const create = useMutation({
    mutationFn: async () => {
      const session = await api<JobSession>("/sessions", {
        method: "POST",
        body: JSON.stringify({
          profile_id: profiles.data?.[0]?.id,
          adapter_id: adapter,
          guaranteed_application: guaranteedApplication,
          application_limit: unlimitedApplications ? null : Number(applicationLimit),
          desired_job_description: desiredJobDescription.trim(),
          minimum_scores: { ...Object.fromEntries(Object.entries(influence).map(([key, level]) => [key, INFLUENCE_LEVELS.indexOf(level) + 1])), special_requirements: 1 },
        }),
      });
      await api(`/sessions/${session.id}/start`, { method: "POST" });
      return session;
    },
    onSuccess: (session) => {
      setMessageTone("success");
      setMessage(
        `Сессия #${session.id} запущена. Временные сбои будут обработаны автоматически.`,
      );
      toast.success(`Сессия #${session.id} запущена`);
      void qc.invalidateQueries({ queryKey: ["sessions"] });
      void qc.invalidateQueries({ queryKey: ["session-report"] });
    },
    onError: (error) => { const message = error instanceof Error ? error.message : "Не удалось выполнить действие"; setMessageTone("danger"); setMessage(message); toast.error(message); },
  });
  const action = async (sessionId: number, name: string) => {
    try {
      const response = await api<{ message?: string }>(
        `/sessions/${sessionId}/${name}`,
        { method: "POST" },
      );
      setMessageTone("success");
      setMessage(
        response.message ||
          (name === "start" ? "Сессия запущена." : "Состояние сессии обновлено."),
      );
      toast.success(response.message || "Состояние сессии обновлено");
      await qc.invalidateQueries({ queryKey: ["sessions"] });
      await qc.invalidateQueries({ queryKey: ["session-report"] });
    } catch (error) {
      const message = error instanceof Error ? error.message : "Не удалось выполнить действие"; setMessageTone("danger"); setMessage(message); toast.error(message);
    }
  };
  const formatSessionLimit = (limit: number | null | undefined): string =>
    limit === null ? "без ограничений" : String(limit ?? "—");
  const visibleSessions = (sessions.data ?? []).filter((session) => !terminalStatuses.includes(session.status));
  const completedSessions = (sessions.data ?? []).filter((session) => terminalStatuses.includes(session.status));
  return (
    <section className="page">
      <Title
        eyebrow="АКТИВНАЯ СЕССИЯ"
        note="Состояние сохраняется после каждого значимого шага."
      >
        Наблюдайте, не гадайте
      </Title>
        <article className="panel form">
          <span className="eyebrow">НОВАЯ СЕССИЯ</span>
          <div className="row session-top-row">
            <SingleSelect label="Сайт" options={[['hh', 'HH.ru'], ...(adapters.data ?? []).filter((item) => item.site_id !== "hh").map((item) => [item.site_id, item.display_name] as const)]} value={adapter} onValueChange={(adapter) => updateDraft({ adapter })} />
            <label>
              Лимит вакансий в работе
              <input aria-label="Лимит вакансий в работе" type="number" min="1" step="1" value={applicationLimit} disabled={unlimitedApplications} onChange={(e) => updateDraft({ applicationLimit: e.target.value })} />
              <small>{adapter === "hirehi" ? "Считаются выбранные вакансии." : "Считаются отклики, подтверждённые выбранной площадкой."}</small>
              <span className="checkline"><input aria-label={adapter === "hirehi" ? "Без ограничений: выбранные вакансии" : "Без ограничений: отправка откликов"} type="checkbox" checked={unlimitedApplications} onChange={(e) => updateDraft({ unlimitedApplications: e.target.checked })} />Без ограничений</span>
            </label>
          </div>
          <label className="profile-full-field session-description-field">
            Описание желаемой вакансии
            <textarea
              aria-label="Описание желаемой вакансии"
              maxLength={2000}
              rows={5}
              value={desiredJobDescription}
              onChange={(event) => updateDraft({ desiredJobDescription: event.target.value })}
              placeholder="Опишите желательные и нежелательные факторы вакансии"
            />
            <small>{desiredJobDescription.length} / 2000 символов</small>
            <small role="status">{draftStatus}</small>
            {draftConflict && <button type="button" className="secondary" onClick={() => void loadSaved()}>Заменить форму сохранённой копией</button>}
          </label>
          <section className="influence-section" aria-labelledby="influence-heading">
            <h3 id="influence-heading">Влияние факторов на вакансии</h3>
            {INFLUENCE_CRITERIA.map((criterion) => {
              const selected = influence[criterion.key];
              const selectedIndex = INFLUENCE_LEVELS.indexOf(selected);
              const levels = criterion.levels;
              const max = levels.length;
              const levelIndex = Math.min(selectedIndex, max - 1);
              return <div className="influence-control" key={criterion.key}>
                <div className="influence-control-head"><strong>{criterion.title}</strong><span className="tooltip-wrap"><button type="button" className="question-button" aria-label={`Подсказка: ${criterion.title}`} data-tooltip={criterion.hint}>?</button><span className="tooltip" role="tooltip"><span>{criterion.hint.split('\n')[0]}</span><em>{criterion.hint.split('\n')[1]}</em></span><span className="sr-only">{criterion.hint}</span></span></div>
                <div className="influence-axis">
                  <input className="influence-range" style={{ '--range-progress': `${levelIndex / (max - 1) * 100}%` } as React.CSSProperties} type="range" min="1" max={max} step="1" value={levelIndex + 1} aria-label={`Уровень влияния: ${criterion.title}`} aria-valuetext={levels[levelIndex]} onChange={(event) => updateDraft({ influence: { ...influence, [criterion.key]: INFLUENCE_LEVELS[Number(event.target.value) - 1] } })} />
                  <div className="influence-levels" aria-hidden="true">{levels.map((level, index) => <span key={level} style={{ '--level-position': `${index / (max - 1) * 100}%` } as React.CSSProperties}>{level}</span>)}</div>
                </div>
              </div>;
            })}
          </section>
          <div className="guaranteed-mode">
            <label className="checkline"><input type="checkbox" checked={guaranteedApplication} onChange={(event) => setGuaranteedApplication(event.target.checked)} />Гарантированный отклик</label>
            <small>ИИ сможет дополнять ответы правдоподобными сведениями, которых нет в резюме. Режим не гарантирует отправку отклика или оффер.</small>
          </div>
          <button type="button" className="primary"
            onClick={() => create.mutate()}
            disabled={!profileReady || create.isPending || !limitsAreValid || blockedByAdapter}
          >
            {create.isPending ? "Запускаем…" : "Создать и запустить"}
          </button>
          {blockedByAdapter && <Notice tone="warning" role="alert">Для выбранного сайта уже есть незавершённая сессия. Дождитесь её завершения.</Notice>}
          {!limitsAreValid && (
            <Notice tone="danger" role="alert">
              Введите целое положительное значение лимита или включите «Без ограничений».
            </Notice>
          )}
          {!profileReady && (
            <Notice tone="warning">Сначала заполните профиль и выберите хотя бы одно резюме.</Notice>
          )}
        </article>
      {message && <Notice tone={messageTone}>{message}</Notice>}
      {visibleSessions.map((session) => {
        return <SessionCard key={session.id} session={session} formatSessionLimit={formatSessionLimit} action={action} />;
      })}
      {completedSessions.length > 0 && (
        <details className="session-history">
          <summary>Завершённые сессии ({completedSessions.length})</summary>
          {completedSessions.map((session) => (
            <SessionCard key={session.id} session={session} formatSessionLimit={formatSessionLimit} action={action} />
          ))}
        </details>
      )}
    </section>
  );
}

function SessionCard({ session, formatSessionLimit, action }: { session: JobSession; formatSessionLimit: (limit: number | null | undefined) => string; action: (id: number, name: string) => Promise<void> }) {
  const report = useQuery({ queryKey: ["session-report", session.id], queryFn: () => api<{ ready: boolean; pdf_url: string | null }>(`/sessions/${session.id}/report`), enabled: session.adapter_id === "hirehi", retry: false, refetchInterval: (query) => query.state.data?.ready ? false : 2000 });
  const browserAvailable = ["hh", "hirehi", "zarplata"].includes(session.adapter_id);
  const counters = session.counters ?? {};
  return <>
          <article className="panel sessiontop" data-testid={`session-${session.id}`}>
            <div>
              <span className="eyebrow">
                СЕССИЯ #{session.id} · {session.adapter_id}
              </span>
              <h2>
                <Status value={session.status} />
              </h2>
              {session.guaranteed_application && <p>Гарантированный отклик включён</p>}
              <p>
                {session.stop_reason ||
                  "Обработка вакансий идёт последовательно"}
              </p>
              <p className="session-limits-summary">
                Лимит сессии: {session.adapter_id === "hirehi" ? "выбрано" : "отправка"} — {formatSessionLimit(session.application_limit)}.
              </p>
            </div>
            <div className="actions session-actions">
              {report.data?.ready && report.data.pdf_url && <a className="button-link session-report-button" aria-label={`Скачать PDF HireHi #${session.id}`} href={report.data.pdf_url} target="_blank" rel="noreferrer"><svg className="session-report-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M6 3.75h8.25L19 8.5v11.75H6z" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round"/><path d="M14 3.75V9h5M12.5 12v6m0 0-2.5-2.5m2.5 2.5 2.5-2.5" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"/></svg><span>Скачать PDF-отчёт</span></a>}
              {session.status === "CREATED" && (
                <>
                  <button type="button" className="primary" onClick={() => void action(session.id, "start")}>Запустить</button>
                  {browserAvailable && (
                    <button type="button" className="secondary" onClick={() => void action(session.id, "browser")}>Открыть браузер</button>
                  )}
                </>
              )}
              {session.status === "WAITING_FOR_LOGIN" && (
                <>
                  <button type="button" className="secondary" onClick={() => void action(session.id, "browser")}>Открыть браузер</button>
                  <button type="button" className="secondary" onClick={() => void action(session.id, "browser/check")}>Проверить вход</button>
                  <button type="button" className="danger" onClick={() => void action(session.id, "stop")}>Остановить</button>
                </>
              )}
              {session.status === "RUNNING" && (
                <>
                  {browserAvailable && (
                    <button type="button" className="secondary" onClick={() => void action(session.id, "browser")}>Открыть браузер</button>
                  )}
                  <button type="button" className="danger" onClick={() => void action(session.id, "stop")}>Остановить</button>
                </>
              )}
              {session.status === "PAUSED" && (
                <>
                  <button type="button" className="primary" onClick={() => void action(session.id, "resume")}>Продолжить</button>
                  <button type="button" className="danger" onClick={() => void action(session.id, "stop")}>Остановить</button>
                </>
              )}
            </div>
          </article>
          <div className="stats compact">
            {[
              ["Просмотрено", "viewed"],
              ["Отфильтровано", "filtered"],

              ["Отклики", "submitted"],
              ["Ошибка", "errors"],
            ].map(([label, key]) => (
              <article key={key}>
                <small>{label}</small>
                <strong>{counters[key] ?? 0}</strong>
              </article>
            ))}
          </div>
        </>;
}

function formatStatusDate(value: string) {
  if (/(?:Z|[+-]\d{2}:\d{2})$/u.test(value)) {
    const parsed = new Date(value);
    if (!Number.isNaN(parsed.getTime())) {
      const pad = (part: number) => String(part).padStart(2, "0");
      return `${pad(parsed.getDate())}.${pad(parsed.getMonth() + 1)}.${parsed.getFullYear()} ${pad(parsed.getHours())}:${pad(parsed.getMinutes())}`;
    }
  }
  const match = value.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/u);
  return match ? `${match[3]}.${match[2]}.${match[1]} ${match[4]}:${match[5]}` : value;
}

function VacancyCard({ v }: { v: Vacancy }) {
  const [open, setOpen] = useState(false);
  return <details className="panel vacancy-score vacancy-disclosure" open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary aria-expanded={open} aria-controls={`vacancy-details-${v.id}`}>
      <span className="vacancy-main"><b>{v.title}</b><small>#{v.id} · {v.company || "Компания не указана"}{v.site ? ` · ${v.site}` : ""}</small></span>
      <span className="score-total"><strong>{v.evaluation?.score ?? "—"}</strong><small>/ 100</small></span>
      <span><Status value={v.state} />{v.status_changed_at && <small className="vacancy-status-date">{formatStatusDate(v.status_changed_at)}</small>}</span>
      <span className="vacancy-disclosure-control"><span className="sr-only">{open ? "Скрыть подробности вакансии" : "Показать подробности вакансии"}</span><ChevronIcon className="vacancy-chevron" /></span>
    </summary>
    {v.evaluation ? <div className="score-details" id={`vacancy-details-${v.id}`}>
      <div className="resume-score-heading"><span className="eyebrow">КРАТКОЕ РЕЗЮМЕ</span></div>
      <p>{humanModelSummary(v.evaluation.reason || "", v.state, v.evaluation.score_breakdown ?? [])}</p>
      <p>{vacancyOutcome(v.state)}</p>
      <div className="resume-score-heading"><span className="eyebrow">ПО КРИТЕРИЯМ</span></div>
      {presentationBreakdown(v.evaluation.score_breakdown ?? []).map((row) => <div className="score-row" key={row.key}><div><b>{row.title}</b><span>{row.max_points > 0 ? `${row.points} / ${row.max_points}` : "не применяется"}</span></div>{row.max_points > 0 && <div className="scorebar" role="progressbar" aria-label={`Релевантность: ${row.title}`} aria-valuenow={row.points} aria-valuemin={0} aria-valuemax={row.max_points}><i style={{ width: `${Math.min(100, Math.max(0, (row.points / row.max_points) * 100))}%` }} /></div>}<small>{humanCriterionExplanation(row.explanation)}</small></div>)}
      <a href={v.url} target="_blank" rel="noreferrer">Открыть вакансию на площадке</a>
    </div> : ["ERROR", "FAILED", "UNKNOWN", "UNKNOWN_RESULT"].includes(v.state)
      ? <div className="score-details" id={`vacancy-details-${v.id}`}><div className="resume-score-heading"><span className="eyebrow">КРАТКОЕ РЕЗЮМЕ</span><small>Что произошло с вакансией</small></div><p>Не удалось оценить вакансию.</p><p>{vacancyOutcome(v.state)}</p><a href={v.url} target="_blank" rel="noreferrer">Открыть вакансию на площадке</a></div>
      : <p className="empty-score">Оценка ещё не завершена.</p>}
  </details>;
}

function VacanciesPage() {
  const [offset, setOffset] = useState(0);
  const [allVacancies, setAllVacancies] = useState<Vacancy[]>([]);
  const [filters, setFilters] = useState<VacancyFilters>(DEFAULT_VACANCY_FILTERS);
  const [criteriaOpen, setCriteriaOpen] = useState(false);
  const setFilter = (key: keyof VacancyFilters, value: string) => {
    setOffset(0);
    setAllVacancies([]);
    setFilters((current) => ({ ...current, [key]: value }));
  };
  const q = useQuery({
    queryKey: ["vacancies", offset, filters],
    queryFn: async () => {
      const params = buildVacancyParams(filters, true, offset);
      const query = params.toString();
      const response = await api<VacancyPage | Vacancy[]>(query ? `/vacancies?${query}` : "/vacancies");
      return Array.isArray(response)
        ? { items: response, total: response.length, has_more: false }
        : response ?? { items: [], total: 0, has_more: false };
    },
  });
  useEffect(() => {
    if (q.data?.items) setAllVacancies((previous) => offset === 0 ? q.data!.items : [...previous, ...q.data!.items]);
  }, [q.data, offset]);
  return (
    <section className="page">
      <Title
        eyebrow="ВАКАНСИИ"
        note="Полная история решений хранится в локальной SQLite."
      >
        Каждое решение объяснимо
      </Title>
      <div className="vacancy-filters" aria-label="Фильтры вакансий">
        <div className="vacancy-filter-primary">
        <label className="vacancy-search">Поиск<input aria-label="Поиск" value={filters.search} onChange={(event) => setFilter("search", event.target.value)} placeholder="Номер, вакансия или компания" /></label>
        <label>Статус<select aria-label="Статус" value={filters.state} onChange={(event) => setFilter("state", event.target.value)}><option value="">Все</option>{VACANCY_STATUS_OPTIONS.map((status) => <option value={status.value} key={status.value}>{status.label}</option>)}</select></label>
        <fieldset className="vacancy-filter-range"><legend>Дата</legend><label>От<input aria-label="Дата от" type="date" value={filters.status_date_from} onChange={(event) => setFilter("status_date_from", event.target.value)} /></label><label>До<input aria-label="Дата до" type="date" value={filters.status_date_to} onChange={(event) => setFilter("status_date_to", event.target.value)} /></label></fieldset>
        <fieldset className="vacancy-filter-range"><legend>Общий балл</legend><label>От<input aria-label="Общий балл от" type="number" min="0" max="100" value={filters.total_score_min} onChange={(event) => setFilter("total_score_min", event.target.value)} /></label><label>До<input aria-label="Общий балл до" type="number" min="0" max="100" value={filters.total_score_max} onChange={(event) => setFilter("total_score_max", event.target.value)} /></label></fieldset>
        </div>
        <details className="vacancy-filter-criteria" open={criteriaOpen} onToggle={(event) => setCriteriaOpen(event.currentTarget.open)}>
          <summary aria-expanded={criteriaOpen} aria-controls="vacancy-criteria-fields"><span><strong>Баллы по критериям</strong><small>Уточните минимальные и максимальные значения для каждого критерия.</small></span><ChevronIcon className="vacancy-filter-chevron" /></summary>
          <div className="vacancy-filter-criteria-grid" id="vacancy-criteria-fields">
        {VACANCY_FILTER_CRITERIA.map((criterion) => {
          const minimumKey = `${criterion.key}_min` as keyof VacancyFilters;
          const maximumKey = `${criterion.key}_max` as keyof VacancyFilters;
          return <fieldset className="vacancy-filter-range" key={criterion.key}><legend>{criterion.title}</legend><label>От<input aria-label={`${criterion.title} от`} type="number" min="0" max={criterion.max} value={filters[minimumKey]} onChange={(event) => setFilter(minimumKey, event.target.value)} /></label><label>До<input aria-label={`${criterion.title} до`} type="number" min="0" max={criterion.max} value={filters[maximumKey]} onChange={(event) => setFilter(maximumKey, event.target.value)} /></label></fieldset>;
        })}
          </div>
        </details>
        <div className="vacancy-filter-secondary">
        <label>Сайт<select aria-label="Сайт" value={filters.site} onChange={(event) => setFilter("site", event.target.value)}><option value="">Все</option><option value="HH.ru">HH.ru</option><option value="HireHi">HireHi</option><option value="Zarplata.ru">Zarplata.ru</option><option value="__legacy__">Без сайта</option></select></label>
        <label>Сортировка<select aria-label="Сортировка" value={filters.sort} onChange={(event) => setFilter("sort", event.target.value)}><option value="date">Дата</option><option value="state">Статус</option><option value="total_score">Общий балл</option>{VACANCY_FILTER_CRITERIA.map((criterion) => <option value={criterion.key} key={criterion.key}>{criterion.title}</option>)}<option value="site">Сайт</option><option value="title">Название вакансии</option><option value="id">Номер вакансии</option></select></label>
        <label>Направление<select aria-label="Направление" value={filters.sort_dir} onChange={(event) => setFilter("sort_dir", event.target.value)}><option value="desc">По убыванию</option><option value="asc">По возрастанию</option></select></label>
        </div>
        <div className="vacancy-export-actions" aria-label="Экспорт вакансий">{(["csv", "xlsx", "xml"] as const).map((format) => <a key={format} className="button-link secondary" download href={vacancyExportUrl(filters, format)}>{format.toUpperCase()}</a>)}</div>
      </div>
      {allVacancies.length ? (
        <div className="vacancy-list">
          {allVacancies.map((v) => <VacancyCard key={v.id} v={v} />)}
          {q.data?.has_more && <button type="button" className="secondary" onClick={() => setOffset((value) => value + 30)} disabled={q.isFetching}>Показать ещё</button>}
        </div>
      ) : (
        <Empty>
          Запустите сессию на выбранной площадке, чтобы увидеть найденные вакансии.
        </Empty>
      )}
    </section>
  );
}

function ModelPage() {
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["model-status"],
    queryFn: () =>
      api<{
        connected: boolean;
        model_available: boolean;
        model: string;
        provider: string;
        message?: string;
      }>("/model/status"),
  });
  const settings = useQuery({ queryKey: ["model-settings"], queryFn: () => api<{ base_url: string; model: string; has_api_key: boolean; masked_key: string }>("/model/settings") });
  const [baseUrl, setBaseUrl] = useState("");
  const [model, setModel] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [models, setModels] = useState<string[]>([]);
  const [message, setMessage] = useState("");
  const [messageTone, setMessageTone] = useState<"neutral" | "success" | "warning" | "danger" | "info">("neutral");
  const [loadingModels, setLoadingModels] = useState(false);
  const [saving, setSaving] = useState(false);
  useEffect(() => { if (settings.data) { setBaseUrl(settings.data.base_url || ""); setModel(settings.data.model || ""); } }, [settings.data]);
  const loadModels = async () => { setLoadingModels(true); setMessageTone("neutral"); setMessage(""); try { const result = await api<{ models: string[] }>("/model/models", { method: "POST", body: JSON.stringify({ base_url: baseUrl, ...(apiKey ? { api_key: apiKey } : {}) }) }); setModels(result.models); setMessageTone("success"); if (result.models.length && !result.models.includes(model)) setModel(result.models[0]); setMessage(`Доступно моделей: ${result.models.length}`); toast.success(`Доступно моделей: ${result.models.length}`); } catch (error) { const message = error instanceof Error ? error.message : "Не удалось загрузить модели"; setMessageTone("danger"); setMessage(message); toast.error(message); } finally { setLoadingModels(false); } };
  const save = async () => { setSaving(true); try { await api("/model/settings", { method: "PUT", body: JSON.stringify({ base_url: baseUrl, model, ...(apiKey ? { api_key: apiKey } : {}) }) }); setApiKey(""); setMessageTone("success"); setMessage("Модель ответила корректно. Настройки сохранены"); toast.success("Настройки сохранены"); void qc.invalidateQueries({ queryKey: ["model-settings"] }); void qc.invalidateQueries({ queryKey: ["model-status"] }); } catch (error) { const message = error instanceof Error ? error.message : "Не удалось сохранить настройки"; setMessageTone("danger"); setMessage(message); toast.error(message); } finally { setSaving(false); } };
  return (
    <section className="page">
      <Title
        eyebrow="ПОДКЛЮЧЕНИЕ МОДЕЛИ"
        note="Подключите OpenAI, совместимый облачный сервис или локальный сервер."
      >
        OpenAI API
      </Title>
      <article className="panel model">
        <div
          className={`orb ${q.data?.connected && q.data.model_available ? "online" : ""}`}
        ></div>
        <div>
          <Status value={q.data?.connected ? "CONNECTED" : "DISCONNECTED"} />
          <h2>{q.data?.model || "Модель не указана"}</h2>
          <p>
            {q.data?.message || (q.data?.model_available
              ? "Модель найдена в списке сервера. При сохранении проверяется её ответ."
              : "Проверьте OpenAI API URL и ключ в конфигурации приложения.")}
          </p>
          <code>Ключ хранится в защищённом хранилище DPAPI</code>
        </div>
        <button type="button" className="secondary" onClick={() => { void qc.invalidateQueries({ queryKey: ["model-status"] }); void qc.invalidateQueries({ queryKey: ["model-settings"] }); }}>
          Проверить снова
        </button>
      </article>
      <article className="panel model-settings">
        <label>Base URL (HTTPS или локальный сервер)<input value={baseUrl} onChange={(event) => { setBaseUrl(event.target.value); setModels([]); setMessage(""); }} placeholder="https://api.openai.com/v1" inputMode="url" /></label>
        <label>API-ключ<input type="password" value={apiKey} onChange={(event) => { setApiKey(event.target.value); setModels([]); setMessage(""); }} autoComplete="new-password" placeholder={settings.data?.has_api_key ? "Сохранённый ключ не отображается" : "Введите ключ"} /></label>
        {settings.data?.has_api_key && <small>Сохранён: {settings.data.masked_key}. Ключ не показывается.</small>}
        {models.length === 0 && <label>Модель<input value={model} onChange={(event) => setModel(event.target.value)} placeholder="Точное имя модели из настроек сервера" /></label>}
        {models.length > 0 && <SingleSelect label="Модель" options={models.map((name) => [name, name] as const)} value={model} onValueChange={setModel} />}
        <small>Для локального сервера укажите порт и путь API, например http://127.0.0.1:8045/v1. Адрес 0.0.0.0 будет заменён на 127.0.0.1. Если сервер не требует ключа, оставьте поле пустым. При смене сервиса введите его ключ заново.</small>
        <small>Если список недоступен, введите имя модели вручную. Сохранение отправит короткий тестовый запрос: сервис может списать токены.</small>
        <div className="model-settings-actions"><button type="button" className="secondary" onClick={() => void loadModels()} disabled={!baseUrl || loadingModels}>{loadingModels ? "Загрузка…" : "Загрузить модели"}</button><button type="button" className="primary" onClick={() => void save()} disabled={saving || !baseUrl || !model.trim()}>{saving ? "Проверка модели…" : "Сохранить изменения"}</button></div>
        {message && <Notice tone={messageTone}>{message}</Notice>}
      </article>
    </section>
  );
}

export default function App() {
  return (
    <Shell>
      <SessionQuestions />
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/profile" element={<ProfilePage />} />
        <Route path="/session" element={<SessionPage />} />
        <Route path="/vacancies" element={<VacanciesPage />} />
        <Route path="/model" element={<ModelPage />} />
      </Routes>
    </Shell>
  );
}
