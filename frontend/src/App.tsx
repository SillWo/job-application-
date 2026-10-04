import { ReactNode, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { NavLink, Route, Routes, useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Toaster, toast } from "sonner";
import { Select } from "@base-ui/react/select";
import { Popover } from "@base-ui/react/popover";
import { api } from "./api";
import { formatUtcTimestampLocal, localDateTimeBounds } from "./vacancyDates";
import { validateVacancyFilters, type VacancyFilterRangeFields } from "./vacancyFilterValidation";
import { useSessionDraft } from "./useSessionDraft";
import type {
  JobSession,
  ResumeAiContext,
  ResumeSnapshot,
  SessionHistoryPage,
  ScoreComponent,
  Vacancy,
  VacancyPage,
  Notification,
  ModelStatus,
  VacancyStatusGroup,
} from "./types";
import {
  previewFromResponse,
  purgeLegacyResumeSourceStorage,
  resumeSourcesFromResponse,
  sourceRecordFromResponse,
  RESUME_SITES,
  questionOptionLabel,
  isResumeQuestionValid,
  resumeQuestions,
  previewSections,
  resumeContactLabel,
  statusLabel,
  resumeDisplayTitle,
  safeResumeUrlLabel,
  publicResumeSourceUrl,
  safeResumeImportUrl,
  type ResumePreview,
  type ResumePreviewResponse,
  type ResumeSourceRecord,
} from "./resumeSources";

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

function evaluationRows(evaluation: Vacancy["evaluation"]): ScoreComponent[] {
  if (!evaluation) return [];
  const source = evaluation.score_breakdown;
  if (Array.isArray(source)) return source;
  return Object.entries(source ?? {}).flatMap(([key, points]) => typeof points === "number"
    ? [{ key, title: key, description: "", max_points: 100, points, explanation: "", evidence: [] }]
    : []);
}

function detailedVacancy(base: Vacancy, detail: Vacancy): Vacancy {
  const evaluation = detail.evaluation;
  const directScore = typeof detail.score === "number" ? detail.score : undefined;
  const directDecision = typeof detail.decision === "string" ? detail.decision : undefined;
  const directConfidence = typeof detail.confidence === "number" ? detail.confidence : undefined;
  const directCategory = typeof detail.category === "string" ? detail.category : undefined;
  const directReason = typeof detail.reason === "string" ? detail.reason : undefined;
  const directBreakdown = detail.score_breakdown ?? undefined;
  if (!evaluation && directScore === undefined && directDecision === undefined && directConfidence === undefined && directCategory === undefined && directReason === undefined && directBreakdown === undefined) return { ...base, ...detail };
  const fallback = base.evaluation;
  return {
    ...base,
    ...detail,
    evaluation: {
      ...evaluation,
      decision: directDecision ?? evaluation?.decision ?? fallback?.decision ?? "",
      score: directScore ?? evaluation?.score ?? fallback?.score ?? 0,
      confidence: directConfidence ?? evaluation?.confidence ?? fallback?.confidence ?? 0,
      category: directCategory ?? evaluation?.category ?? fallback?.category ?? "",
      // The detail endpoint returns evaluation fields directly.  Keep the
      // legacy `data` bag opaque; it must never become the presentation source.
      reason: directReason ?? evaluation?.reason ?? fallback?.reason,
      score_breakdown: directBreakdown ?? evaluation?.score_breakdown ?? fallback?.score_breakdown ?? [],
    },
  };
}

function presentationBreakdown(rows: ScoreComponent[] | Record<string, number | null>): ScoreComponent[] {
  const normalizedRows: ScoreComponent[] = Array.isArray(rows) ? rows : Object.entries(rows).flatMap(([key, points]) => typeof points === "number" ? [{ key, title: key, description: "", max_points: 100, points, explanation: "", evidence: [] }] : []);
  return RELEVANCE_CRITERIA.map((criterion) => {
    const legacyKey = criterion.key === "experience_depth" ? "required_years" : criterion.key === "role_match" ? "title" : criterion.key === "special_requirements" ? "languages" : null;
    const row = normalizedRows.find((candidate) => candidate.key === criterion.key) ?? (legacyKey ? normalizedRows.find((candidate) => candidate.key === legacyKey) : undefined);
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
  CREATED: { label: "Создана", tone: "neutral" }, RUNNING: { label: "В работе", tone: "success" }, PAUSED: { label: "Приостановлена", tone: "warning" }, STOPPED: { label: "Остановлена", tone: "neutral" }, COMPLETED: { label: "Завершена", tone: "success" }, FAILED: { label: "Ошибка", tone: "danger" },
  SUCCESS: { label: "Успех", tone: "success" }, PROCESSING: { label: "В процессе", tone: "info" }, REJECTED: { label: "Отклонена", tone: "danger" }, ERROR: { label: "Ошибка", tone: "danger" },
  CONNECTED: { label: "Соединение есть", tone: "success" }, DISCONNECTED: { label: "Нет соединения", tone: "danger" },
  AVAILABLE: { label: "Модель доступна", tone: "success" },
  UNAVAILABLE: { label: "Модель недоступна", tone: "danger" },
  HEALTHY: { label: "Генерация работает", tone: "success" },
  UNHEALTHY: { label: "Ошибка генерации", tone: "danger" },
  PENDING: { label: "Ожидание генерации", tone: "info" },
  UNKNOWN: { label: "Ещё не проверено", tone: "neutral" },
  STATUS_ERROR: { label: "Статус недоступен", tone: "danger" },
  LOADING: { label: "Проверяем", tone: "neutral" },
};
Object.assign(STATUS_META, {
  PREPARING: { label: "\u041f\u043e\u0434\u0433\u043e\u0442\u043e\u0432\u043a\u0430", tone: "neutral" },
  STOPPING: { label: "\u041e\u0441\u0442\u0430\u043d\u043e\u0432\u043a\u0430", tone: "warning" },
  CANCELLED: { label: "\u041e\u0442\u043c\u0435\u043d\u0435\u043d\u0430", tone: "neutral" },
});
const TERMINAL_SESSION_STATUSES = ["COMPLETED", "STOPPED", "FAILED", "CANCELLED"];
type VacancyFilters = {
  search: string;
  status_group: string;
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
  status_group: "",
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
    if (key === "status_date_from" || key === "status_date_to") return;
    const isDefaultSort = key === "sort" && value === DEFAULT_VACANCY_FILTERS.sort;
    const isDefaultDirection = key === "sort_dir" && value === DEFAULT_VACANCY_FILTERS.sort_dir;
    if (value && !isDefaultSort && !isDefaultDirection) params.set(key, value);
  });
  const localDateBounds = localDateTimeBounds(filters.status_date_from, filters.status_date_to);
  if (localDateBounds.from) params.set("status_time_from", localDateBounds.from);
  if (localDateBounds.before) params.set("status_time_before", localDateBounds.before);
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
function localizeNotificationText(value: string): string {
  const statusCodes = "CREATED|PREPARING|RUNNING|STOPPING|STOPPED|PAUSED|COMPLETED|FAILED|CANCELLED";
  const lifecycle = value.match(new RegExp(`^Сессия\\s+#?(\\d+):\\s*статус изменён на\\s+(${statusCodes})$`, "iu"));
  if (lifecycle) return `Сессия #${lifecycle[1]}: состояние изменилось — ${humanStatus(lifecycle[2].toUpperCase()).toLocaleLowerCase()}.`;
  const vacancyStatuses: Record<string, string> = {
    ERROR: "ошибка",
    SUBMITTED: "отправлено",
    REPORTED: "добавлена в отчёт",
    ALREADY_APPLIED: "уже откликались",
    REJECTED_BY_MODEL: "отклонена",
    EXTRACTED: "обрабатывается",
    EVALUATING: "обрабатывается",
    READY_TO_SUBMIT: "обрабатывается",
    READY_TO_REPORT: "обрабатывается",
    SUBMITTING: "обрабатывается",
  };
  const vacancyStatus = value.match(/(статус\s+)(ERROR|SUBMITTED|REPORTED|ALREADY_APPLIED|REJECTED_BY_MODEL|EXTRACTED|EVALUATING|READY_TO_SUBMIT|READY_TO_REPORT|SUBMITTING)\b/iu);
  if (vacancyStatus) return value.replace(vacancyStatus[0], `${vacancyStatus[1]}${vacancyStatuses[vacancyStatus[2].toUpperCase()]}`);
  return value.replace(new RegExp(`\\b(${statusCodes})\\b`, "gu"), (status) => humanStatus(status));
}
function parseServerUtc(value: string | null | undefined): Date | null {
  if (!value) return null;
  const candidate = /(?:Z|[+-]\d{2}:?\d{2})$/u.test(value) ? value : `${value}Z`;
  const parsed = new Date(candidate);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}
function formatServerUtc(value: string | null | undefined): string {
  const parsed = parseServerUtc(value);
  return parsed ? parsed.toLocaleString("ru-RU") : value || "";
}
function formatSessionDuration(session: JobSession): string | null {
  const start = parseServerUtc(session.stage_started_at || session.started_at);
  if (!start) return null;
  const end = parseServerUtc(session.finished_at) ?? new Date();
  const seconds = Math.max(0, Math.floor((end.getTime() - start.getTime()) / 1000));
  if (seconds < 60) return `${seconds} с`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes} мин ${seconds % 60} с`;
}
function safeSessionText(value: string) { return value.replace(/https?:\/\/\S+/giu, "[ссылка скрыта]"); }
function sessionReasonText(value: string | null | undefined): string {
  if (!value?.trim()) return "";
  const normalized = value.trim().toLocaleLowerCase().replace(/[\s-]+/gu, "_");
  if (["user", "user_stop", "stopped_by_user", "user_requested"].includes(normalized)) return "Остановлено пользователем.";
  if (["cancel", "cancelled", "canceled", "user_cancel", "user_cancelled", "user_canceled", "cancelled_by_user", "canceled_by_user"].includes(normalized)) return "Отменено пользователем.";
  const runtimeReasons: Record<string, string> = {
    "worker process exited unexpectedly": "Рабочий процесс аварийно завершился.",
    "workflow returned before terminal session state": "Обработка завершилась без итогового состояния сессии.",
    "worker reported completed before durable session completion": "Рабочий процесс сообщил о завершении раньше, чем оно было сохранено.",
    "worker reported paused while the session was not paused": "Рабочий процесс сообщил о паузе, хотя сессия не была приостановлена.",
    "worker process containment could not be established": "Не удалось изолировать рабочий процесс.",
  };
  const runtimeReason = runtimeReasons[value.trim().toLocaleLowerCase()];
  if (runtimeReason) return runtimeReason;
  return safeSessionText(value);
}
function isCaptchaPause(value: string | null | undefined) { return Boolean(value && /captcha|капч/iu.test(value)); }
function isAuthorizationPause(value: string | null | undefined) { return Boolean(value && /auth|авторизац|вход|логин|login/iu.test(value)); }
function pausedSessionMessage(reason: string | null | undefined) {
  if (isCaptchaPause(reason)) return "Пауза CAPTCHA: пройдите проверку в открытом браузере, затем нажмите «Продолжить».";
  if (isAuthorizationPause(reason)) return "Пауза авторизации: войдите на площадку в открытом браузере, затем нажмите «Продолжить».";
  return "Сессия приостановлена. Проверьте CAPTCHA или авторизацию в открытом браузере, затем нажмите «Продолжить».";
}
const VACANCY_STATUS_OPTIONS = [
  { value: "SUCCESS", label: "Успех" }, { value: "PROCESSING", label: "В процессе" }, { value: "REJECTED", label: "Отклонена" }, { value: "ERROR", label: "Ошибка" },
] as const;
const VACANCY_STATUS_OPTIONS_WITH_CANCELLED = [...VACANCY_STATUS_OPTIONS, { value: "CANCELLED", label: "\u041e\u0442\u043c\u0435\u043d\u0435\u043d\u0430" }] as const;
function vacancyStatusGroup(vacancy: Vacancy): VacancyStatusGroup {
  const dataErrorCode = typeof vacancy.data?.error_code === "string" ? vacancy.data.error_code : undefined;
  const legacyStatusGroup = (vacancy as { status_group?: unknown }).status_group;
  if (vacancy.error_code === "SUBMISSION_UNCONFIRMED" || dataErrorCode === "SUBMISSION_UNCONFIRMED" || vacancy.state === "SUBMISSION_UNCONFIRMED" || vacancy.state === "UNCONFIRMED" || legacyStatusGroup === "UNCONFIRMED") return "ERROR";
  if (vacancy.state === "CANCELLED" || vacancy.status_group === "CANCELLED") return "CANCELLED";
  if (vacancy.status_group === "SUCCESS" || vacancy.status_group === "PROCESSING" || vacancy.status_group === "REJECTED" || vacancy.status_group === "ERROR") {
    return vacancy.status_group;
  }
  if (["SUBMITTED", "ALREADY_APPLIED", "REPORTED"].includes(vacancy.state)) return "SUCCESS";
  if (vacancy.state === "REJECTED_BY_MODEL") return "REJECTED";
  if (vacancy.state === "ERROR") return "ERROR";
  return "PROCESSING";
}
function vacancyOutcome(vacancy: Vacancy) {
  if (vacancyStatusGroup(vacancy) === "CANCELLED") return "Обработка вакансии отменена до завершения.";
  if (vacancy.state === "SUBMITTED") return "Отклик действительно отправлен после положительной оценки вакансии.";
  if (vacancy.state === "ALREADY_APPLIED") return "Новый отклик не отправлялся: вы уже откликались на эту вакансию.";
  if (vacancy.state === "REPORTED") return "Вакансия добавлена в отчёт, внешний отклик не отправлялся.";
  if (vacancyStatusGroup(vacancy) === "REJECTED") return "Модель отклонила вакансию из-за недостаточной релевантности, поэтому отклик не отправлен.";
  if (vacancyStatusGroup(vacancy) === "ERROR") return "Результат обработки вакансии не подтверждён.";
  return "Обработка вакансии ещё идёт.";
}
function vacancyStatusLabel(vacancy: Vacancy): string | undefined {
  const site = (vacancy.site || vacancy.source || "").toLowerCase();
  if (vacancy.state === "REPORTED" && (site === "hirehi" || site === "hirehi.ru")) return "\u0412 \u043e\u0442\u0447\u0451\u0442\u0435";
  if (vacancy.state === "SUBMITTED" && site && !site.includes("hirehi")) return "\u041e\u0442\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u043e";
  return undefined;
}
type ModelHealthPresentation = {
  code: string;
  label: string;
  detail: string;
  complete: boolean;
};
function catalogHealthPresentation(
  status: ModelStatus | undefined,
  loading: boolean,
  failed: boolean,
): ModelHealthPresentation {
  if (loading) return { code: "LOADING", label: "Проверяем каталог", detail: "Запрашиваем список моделей у сервиса.", complete: false };
  if (failed || !status) return { code: "STATUS_ERROR", label: "Статус каталога недоступен", detail: "Не удалось проверить каталог. Повторите попытку.", complete: false };
  if (status.connected && status.model_available) return { code: "AVAILABLE", label: "Модель доступна в каталоге", detail: status.message || "Сервис отвечает, выбранная модель есть в каталоге.", complete: true };
  if (status.connected) return { code: "UNAVAILABLE", label: "Модель не найдена в каталоге", detail: status.message || "Сервис отвечает, но выбранная модель не найдена в его каталоге.", complete: false };
  return { code: "UNAVAILABLE", label: "Каталог недоступен", detail: status.message || "Проверьте адрес сервиса, ключ и подключение.", complete: false };
}
function generationHealthPresentation(
  status: ModelStatus | undefined,
  loading: boolean,
  failed: boolean,
): ModelHealthPresentation {
  if (loading) return { code: "LOADING", label: "Проверяем историю генерации", detail: "Загружаем результаты реальных запросов.", complete: false };
  if (failed || !status) return { code: "STATUS_ERROR", label: "Статус генерации недоступен", detail: "Историю реальных запросов сейчас прочитать не удалось.", complete: false };
  const health = status.generation_health;
  if (!health) return { code: "UNKNOWN", label: "Генерация ещё не проверена", detail: "Реальных запросов к модели ещё не было.", complete: false };
  if (health.healthy === true) {
    const activity = health.running > 0
      ? `Сейчас выполняется запросов: ${health.running}.`
      : health.queued > 0
        ? `Рабочие слоты заняты, в очереди: ${health.queued}.`
        : "Последний реальный запрос завершился успешно.";
    return { code: "HEALTHY", label: "Генерация работает", detail: activity, complete: true };
  }
  if (health.healthy === false) {
    const activity = health.running > 0
      ? `После ошибки уже выполняется новая проверка: ${health.running}.`
      : health.queued > 0
        ? `После ошибки запросы ожидают свободный рабочий слот: ${health.queued}.`
        : "Последний реальный запрос завершился ошибкой. Каталог при этом может оставаться доступным.";
    return { code: "UNHEALTHY", label: "Генерация требует проверки", detail: activity, complete: false };
  }
  if (health.running > 0) return { code: "PENDING", label: "Генерация выполняется", detail: `Активных запросов: ${health.running}. Результата ещё нет.`, complete: false };
  if (health.queued > 0) return { code: "PENDING", label: "Генерация ожидает очереди", detail: `Запросов в очереди: ${health.queued}. Они начнутся после освобождения рабочего слота.`, complete: false };
  return { code: "UNKNOWN", label: "Генерация ещё не проверена", detail: "Каталог может быть доступен, но успешных или неуспешных генераций ещё не было.", complete: false };
}
function vacancyErrorCode(vacancy: Vacancy): string {
  const dataErrorCode = typeof vacancy.data?.error_code === "string" ? vacancy.data.error_code.trim() : "";
  return vacancy.error_code?.trim() || dataErrorCode || "VACANCY_PROCESSING_FAILED";
}
function vacancyErrorMessage(vacancy: Vacancy): string {
  const dataErrorCode = typeof vacancy.data?.error_code === "string" ? vacancy.data.error_code : undefined;
  const dataErrorMessage = typeof vacancy.data?.error_message === "string" ? vacancy.data.error_message : undefined;
  const legacyStatusGroup = (vacancy as { status_group?: unknown }).status_group;
  if (vacancy.error_code === "SUBMISSION_UNCONFIRMED" || dataErrorCode === "SUBMISSION_UNCONFIRMED" || vacancy.state === "SUBMISSION_UNCONFIRMED" || vacancy.state === "UNCONFIRMED" || legacyStatusGroup === "UNCONFIRMED") {
    return "После повторных попыток не удалось подтвердить отправку.";
  }
  return vacancy.error_message?.trim() || dataErrorMessage?.trim() || "Не удалось завершить обработку вакансии.";
}
function humanRelevanceReason(reason: string, group: VacancyStatusGroup) {
  const cleaned = reason.replace(/\s*(?:Ограничения|Restrictions)\s*:.*/is, "").replace(/\s*(?:Evidence не прошло локальную лексическую проверку|grounding warning).*/i, "").trim();
  const legacyGeneric = cleaned === "Оценка вакансии на основе резюме.";
  if (!cleaned || legacyGeneric || /(?:минимум|minimum|score|confidence|evidence|threshold|flag|raw[_ ]?points|raw[_ ]?fields|grounding|локальную лексическую проверку)/i.test(cleaned) || /\d+\s*\/\s*\d+/.test(cleaned)) {
    if (group === "SUCCESS") return "Вакансия в целом соответствует вашему профилю.";
    if (group === "ERROR") return "Вакансия оценена по резюме, но обработка не завершилась.";
    return group === "REJECTED" ? "Вакансия недостаточно релевантна вашему профилю." : "Вакансия оценена по вашему резюме.";
  }
  return cleaned;
}
function humanCriterionExplanation(explanation: string) {
  const cleaned = explanation.replace(/\s*(?:Evidence не прошло локальную лексическую проверку|grounding warning).*/i, "").trim();
  return !cleaned || /(?:минимум|minimum|score|confidence|evidence|threshold|flag|raw[_ ]?points|raw[_ ]?fields|grounding)/i.test(cleaned) || /\d+\s*\/\s*\d+/.test(cleaned) ? "По этому критерию дополнительных пояснений нет." : cleaned;
}
function humanModelSummary(reason: string, group: VacancyStatusGroup, rows: ScoreComponent[]) {
  const cleanedReason = humanRelevanceReason(reason, group);
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
function Status({ value, label }: { value: string; label?: string }) {
  const meta = STATUS_META[value];
  return (
    <span className={`status status-${meta?.tone ?? "neutral"} s-${value.toLowerCase()}`} data-status={value}>
      {label ?? humanStatus(value)}
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

export function Notifications() {
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const seenEventIds = useRef<Set<string> | null>(null);
  const query = useQuery({ queryKey: ["notifications"], queryFn: async () => { const value = await api<unknown>("/notifications"); return Array.isArray(value) ? value as Notification[] : []; }, refetchInterval: import.meta.env.MODE === "test" ? false : 2500, refetchIntervalInBackground: false, retry: false });
  // Keep the client ordering identical to the API ordering.  The id tie-breaker
  // is important when SQLite timestamps have the same precision (and also keeps
  // malformed/missing timestamps from making the order unstable).
  const notifications = [...(Array.isArray(query.data) ? query.data : [])].sort((a, b) => {
    const byDate = (parseServerUtc(b.created_at)?.getTime() || 0) - (parseServerUtc(a.created_at)?.getTime() || 0);
    return byDate || b.id - a.id;
  });
  const unread = notifications.filter((item) => !item.read_at).length;
  useEffect(() => {
    if (!query.data) return;
    const currentEventIds = new Set(query.data.map((item) => item.event_id || String(item.id)));
    const newEvents = seenEventIds.current ? query.data.filter((item) => !seenEventIds.current!.has(item.event_id || String(item.id))) : [];
    if (seenEventIds.current && newEvents.length > 0) {
      for (const item of newEvents) {
        toast(localizeNotificationText(item.title), { id: `notification-${item.event_id || item.id}`, description: localizeNotificationText(item.message) });
      }
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
    if (!seenEventIds.current) seenEventIds.current = currentEventIds;
    else currentEventIds.forEach((eventId) => seenEventIds.current!.add(eventId));
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
      {query.isLoading ? <p className="notification-empty">Загрузка…</p> : query.error ? <p className="notification-empty notification-error">Не удалось загрузить уведомления.</p> : notifications.length === 0 ? <p className="notification-empty">Новых уведомлений нет</p> : <div className="notification-list">{notifications.map((item) => <button type="button" className={`notification-item${item.read_at ? "" : " unread"}`} key={item.id} onClick={() => void markRead(item)}><strong>{localizeNotificationText(item.title)}</strong><span>{localizeNotificationText(item.message)}</span><time dateTime={item.created_at}>{formatServerUtc(item.created_at)}</time></button>)}</div>}
    </Popover.Popup></Popover.Positioner></Popover.Portal>
  </div></Popover.Root>;
}
async function fetchResumeSources(): Promise<Record<string, ResumeSourceRecord>> {
  return resumeSourcesFromResponse(await api<unknown>("/resume-sources"));
}

function Dashboard() {
  const resumeSourcesQuery = useQuery({
    queryKey: ["resume-sources"],
    queryFn: () => fetchResumeSources(),
    staleTime: 30_000,
    refetchInterval: false,
    refetchOnWindowFocus: false,
  });
  const resumeSources = resumeSourcesQuery.data ?? {};
  const sessions = useQuery({
    queryKey: ["sessions"],
    queryFn: () => api<JobSession[]>("/sessions"),
  });
  const modelStatus = useQuery({
    queryKey: ["model-status"],
    queryFn: () => api<ModelStatus>("/model/status"),
    retry: false,
  });
  const catalogReadiness = catalogHealthPresentation(modelStatus.data, modelStatus.isLoading, modelStatus.isError);
  const generationReadiness = generationHealthPresentation(modelStatus.data, modelStatus.isLoading, modelStatus.isError);
  const active = sessions.data?.find((session) => !TERMINAL_SESSION_STATUSES.includes(session.status));
  const hasProfile = Object.keys(resumeSources).length > 0;
  // HH/Zarplata must have a ready local copy. HireHi retains its confirmed
  // source flow, including the availability check made by each new launch.
  const hasResume = Object.values(resumeSources).some((source) => source.confirmed && ((source.adapterId !== "hh" && source.adapterId !== "zarplata") || source.resumeDataStatus === "ready"));
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
          <p>Добавьте ссылки на резюме с площадок, а затем поручите ИИ найти и разобрать вакансии по заданным критериям и лимитам. Вы наблюдаете за процессом и управляете условиями поиска.</p>
          <div className="overview-actions">
            <NavLink className="button-link overview-primary overview-quiet-control" to={primaryHref}>{primaryLabel} <OverviewIcon name="arrow" /></NavLink>
            <NavLink className="overview-text-link overview-quiet-link" to={secondaryHref}>{secondaryLabel}</NavLink>
          </div>
          <div className="overview-status-strip" aria-label="Готовность к поиску">
            <span className={catalogReadiness.complete ? "is-done" : ""}><OverviewIcon name="check" />{catalogReadiness.label}</span>
            <span className={generationReadiness.complete ? "is-done" : ""}><OverviewIcon name="check" />{generationReadiness.label}</span>
            <span className={hasProfile ? "is-done" : ""}><OverviewIcon name="check" />Источники {hasProfile ? "добавлены" : "не настроены"}</span>
            <span className={hasResume ? "is-done" : ""}><OverviewIcon name="check" />Резюме {hasResume ? "подтверждено" : "не подтверждено"}</span>
            <span><i key={`status-pulse-strip-${active?.status ?? "none"}`} className={sessionReady ? "overview-status-pulse" : ""} />Сессия {activeStatus.toLowerCase()}</span>
          </div>
        </div>
        <div className="overview-preview" aria-label="Статус рабочего процесса">
          <div className="preview-top"><span key={`status-pulse-preview-${active?.status ?? "none"}`} className={`preview-dot${sessionReady ? " overview-status-pulse" : ""}`} />Рабочий процесс <span className="preview-live">{activeStatus}</span></div>
          <div className="preview-job"><span className="preview-logo">J</span><div><strong>Подходящие вакансии</strong><small>Оценка по резюме и условиям сессии</small></div><b>{sessionReady ? "Оценивает" : active ? activeStatus : hasResume ? "Готово к запуску" : "Нужно настроить"}</b></div>
          <div className="preview-lines">
            <div className="preview-line"><i className={hasProfile ? "is-done" : ""}>{hasProfile ? "✓" : "1"}</i><span>Источники резюме</span><small>{hasProfile ? "Добавлены" : "Нужно настроить"}</small></div>
            <div className="preview-line"><i className={hasResume ? "is-done" : ""}>{hasResume ? "✓" : "2"}</i><span>Подтверждённое резюме</span><small>{hasResume ? "Готово к оценке" : "Добавьте ссылку"}</small></div>
            <div className="preview-line"><i className={sessionReady ? "is-done" : ""}>{sessionReady ? "✓" : "3"}</i><span>Наблюдение за сессией</span><small>{active ? activeStatus : "Настройте критерии и лимиты"}</small></div>
          </div>
          {active && <div className="preview-proof">
            <div className="preview-proof-head"><div><span>Вакансия оценивается</span><h2>Продуктовая роль</h2><p>Сопоставление с выбранным резюме</p></div><b>Разбор</b></div>
            <div className="preview-proof-list">
              <div><OverviewIcon name="fileCheck" /><p><strong>Опыт и задачи</strong><span>ИИ ищет подтверждение требований в опыте кандидата.</span></p></div>
              <div><OverviewIcon name="sliders" /><p><strong>Критерии сессии</strong><span>Формат, роль и ограничения учитываются при оценке.</span></p></div>
              <div><OverviewIcon name="scan" /><p><strong>Результат для просмотра</strong><span>Причины соответствия остаются видимыми пользователю.</span></p></div>
            </div>
          </div>}
        </div>
      </div>
    </section>
  </div>;
}

function resumeSite(adapterId: string) {
  return RESUME_SITES.find((site) => site.id === adapterId) ?? RESUME_SITES[0];
}

function resumeEditUrl(adapterId: string): string {
  // Keep the action useful without putting the bearer resume URL (or its
  // opaque id) into the DOM, session storage, telemetry, or copied markup.
  return adapterId === "hh"
    ? "https://hh.ru/applicant/resumes"
    : adapterId === "hirehi"
      ? "https://hirehi.ru/profile/resumes"
      : "https://zarplata.ru/profile/resumes";
}

function isAllowedResumeUrl(adapterId: string, raw: string): boolean {
  try {
    const url = new URL(raw.trim());
    // The saved source identity is queryless; reject tracking/import variants
    // before the preview request so the server can return a separate import URL.
    if (url.protocol !== "https:" || url.username || url.password || url.port || url.search || url.hash) return false;
    const host = url.hostname.toLowerCase();
    const hostAllowed = adapterId === "hh"
      ? host === "hh.ru" || host.endsWith(".hh.ru")
      : adapterId === "hirehi" ? host === "hirehi.ru" || host === "www.hirehi.ru" : host === "zarplata.ru" || host.endsWith(".zarplata.ru");
    return hostAllowed && /^\/resume\/[^/]+\/?$/u.test(url.pathname);
  } catch { return false; }
}

function sanitizeResumePreview(preview: ResumePreview): ResumePreview {
  const { source_resume_id: _sourceResumeId, source_edit_url: _sourceEditUrl, ...safePreview } = preview;
  void _sourceResumeId;
  void _sourceEditUrl;
  return safePreview;
}

function sourceStatusLabel(record: ResumeSourceRecord | undefined, checking = false): string {
  if (checking) return "Проверяем…";
  if (!record) return "Не настроено";
  return record.status === "changed" ? "Обновлено" : record.status === "unavailable" ? "Недоступно" : "Актуально";
}

function ResumePreviewCard({ adapterId, record, checking, onConfirmed, onPreferenceChange, onRemoved, onRefreshed }: { adapterId: string; record: ResumeSourceRecord | undefined; checking?: boolean; onConfirmed: (record: ResumeSourceRecord) => void; onPreferenceChange: (adapterId: string, grammaticalGender: "male" | "female") => Promise<void>; onRemoved: (adapterId: string) => void; onRefreshed: (record: ResumeSourceRecord) => void }) {
  const site = resumeSite(adapterId);
  const [url, setUrl] = useState("");
  const savedPreview = record?.preview && Object.keys(record.preview).length > 0 ? record.preview : null;
  const [preview, setPreview] = useState<ResumePreview | null>(savedPreview);
  const [token, setToken] = useState("");
  const [consent, setConsent] = useState(false);
  const [editing, setEditing] = useState(false);
  const [deletePending, setDeletePending] = useState(false);
  const [resumeAnswers, setResumeAnswers] = useState<Record<string, string>>(() => {
    const answers: Record<string, string> = {};
    if (record?.grammaticalGender) answers.grammatical_gender = record.grammaticalGender;
    return answers;
  });
  const [error, setError] = useState("");
  useEffect(() => {
    if (!editing) {
      setPreview(record?.preview && Object.keys(record.preview).length > 0 ? record.preview : null);
      setResumeAnswers(record?.grammaticalGender ? { grammatical_gender: record.grammaticalGender } : {});
    }
  }, [record?.adapterId, record?.checkedAt, record?.status, record?.preview, record?.grammaticalGender, editing]);
  const previewMutation = useMutation({
    mutationFn: () => {
      if (!isAllowedResumeUrl(adapterId, url)) throw new Error(`Укажите безопасную прямую ссылку на резюме ${site.label} (HTTPS, без параметров).`);
      return api<{ preview_token: string; preview?: ResumePreview; public_preview?: ResumePreview; source?: ResumePreview }>("/resume-sources/preview", { method: "POST", body: JSON.stringify({ adapter_id: adapterId, resume_url: url.trim() }) });
    },
    onSuccess: (result) => {
      const nextPreview = sanitizeResumePreview(previewFromResponse(result));
      if (!result.preview_token) { setPreview(null); setToken(""); setError("Сервис не выдал одноразовый токен проверки. Повторите попытку."); return; }
      if (nextPreview.source_site && nextPreview.source_site !== adapterId) {
        setPreview(null); setToken(""); setError("Сайт в ответе не совпал с выбранной площадкой. Проверьте ссылку ещё раз."); return;
      }
      setPreview(nextPreview); setToken(result.preview_token); setConsent(false); setEditing(true); setError(""); setUrl("");
      setResumeAnswers({});
    },
    onError: (reason) => setError(reason instanceof Error ? reason.message : "Не удалось проверить ссылку."),
  });
  const detailsMutation = useMutation({
    mutationFn: () => {
      if (!token) throw new Error("Предпросмотр устарел.");
      return api<ResumePreviewResponse>("/resume-sources/preview-details", {
        method: "POST",
        body: JSON.stringify({ preview_token: token }),
        cache: "no-store",
      });
    },
    onSuccess: (result) => {
      const details = sanitizeResumePreview(previewFromResponse(result));
      setPreview((current) => ({ ...(current ?? {}), ...details }));
      setError("");
    },
    onError: (reason) => setError(reason instanceof Error ? reason.message : "Не удалось показать подробности."),
  });
  const confirmMutation = useMutation({
    mutationFn: () => api<unknown>("/resume-sources/confirm", { method: "POST", body: JSON.stringify({ adapter_id: adapterId, preview_token: token, consent: true, ...(resumeAnswers.grammatical_gender ? { grammatical_gender: resumeAnswers.grammatical_gender } : {}) }) }),
    onSuccess: (result) => {
      const serverRecord = sourceRecordFromResponse(result);
      const grammaticalGender = serverRecord?.grammaticalGender ?? (resumeAnswers.grammatical_gender === "male" || resumeAnswers.grammatical_gender === "female" ? resumeAnswers.grammatical_gender : null);
      const next = serverRecord
        ? { ...serverRecord, grammaticalGender, preview: Object.keys(serverRecord.preview).length ? serverRecord.preview : preview! }
        : { adapterId, usesSavedData: adapterId === "hh" || adapterId === "zarplata", resumeDataStatus: adapterId === "hh" || adapterId === "zarplata" ? "missing" as const : null, resumeDataSavedAt: null, resumeDataErrorMessage: null, grammaticalGender, preview: preview!, previewToken: token, confirmed: true, status: "valid" as const };
      setEditing(false); setConsent(false); setError(""); onConfirmed(next);
    },
    onError: (reason) => setError(reason instanceof Error ? reason.message : "Не удалось сохранить резюме."),
  });
  const hasSavedSource = Boolean(record);
  const questions = preview ? resumeQuestions(preview) : [];
  // The durable preference is intentionally stripped from the public preview
  // once saved, so keep a small local editor on the source card as well.
  const profileQuestions = hasSavedSource && !questions.some((question) => question.id === "grammatical_gender")
    ? [{ id: "grammatical_gender", question: "Какой род использовать в сопроводительных письмах?", options: ["male", "female"], required: true }, ...questions]
    : questions;
  const confirm = () => {
    if (!preview || !token || !consent) return;
    if (!profileQuestions.every((question) => isResumeQuestionValid(question, resumeAnswers[question.id] ?? ""))) return;
    confirmMutation.mutate();
  };
  const removeMutation = useMutation({
    mutationFn: () => api<unknown>(`/resume-sources/${adapterId}`, { method: "DELETE" }),
    onSuccess: () => { setPreview(null); setToken(""); setConsent(false); setEditing(false); setDeletePending(false); setUrl(""); setError(""); onRemoved(adapterId); },
    onError: (reason) => setError(reason instanceof Error ? reason.message : "Не удалось удалить источник."),
  });
  const refreshMutation = useMutation({
    mutationFn: () => api<unknown>(`/resume-sources/${adapterId}/refresh`, { method: "POST" }),
    onSuccess: (result) => {
      const refreshed = sourceRecordFromResponse(result);
      if (!refreshed) { setError("Не удалось обновить данные резюме."); return; }
      onRefreshed({ ...refreshed, preview: Object.keys(refreshed.preview).length ? refreshed.preview : record?.preview ?? {} });
      setError(refreshed.status === "unavailable"
        ? refreshed.resumeDataErrorMessage || "Обновление не удалось. Последняя сохранённая копия данных остаётся доступной."
        : "");
    },
    onError: (reason) => setError(reason instanceof Error ? reason.message : "Не удалось обновить данные резюме."),
  });
  const remove = () => {
    removeMutation.mutate();
  };
  const beginReplace = () => {
    setEditing(true); setPreview(null); setToken(""); setConsent(false); setDeletePending(false); setError(""); setResumeAnswers({});
  };
  const cancelReplace = () => {
    setPreview(record?.preview && Object.keys(record.preview).length > 0 ? record.preview : null);
    setToken("");
    setConsent(false);
    setResumeAnswers(record?.grammaticalGender ? { grammatical_gender: record.grammaticalGender } : {});
    setEditing(false);
    setDeletePending(false);
    setError("");
  };
  const updateResumeAnswer = (questionId: string, value: string) => {
    setResumeAnswers((current) => ({ ...current, [questionId]: value }));
    if (record?.confirmed && !editing) {
      if (questionId === "grammatical_gender" && (value === "male" || value === "female")) {
        void onPreferenceChange(adapterId, value).catch((reason) => setError(reason instanceof Error ? reason.message : "Не удалось сохранить предпочтение."));
      }
    }
  };
  const isUnavailable = record?.status === "unavailable";
  const usesSavedData = adapterId === "hh" || adapterId === "zarplata";
  const savedSourceUrl = publicResumeSourceUrl(record?.sourceUrl, adapterId)
    ?? publicResumeSourceUrl(record?.preview.source_url, adapterId);
  const savedAddress = savedSourceUrl
    ?? (record?.maskedUrl || record?.preview.masked_url
      ? safeResumeUrlLabel(record.maskedUrl ?? record.preview.masked_url)
      : "Ссылка сохранена");
  const savedImportUrl = safeResumeImportUrl(record?.importUrl, adapterId, savedSourceUrl)
    ?? safeResumeImportUrl(record?.preview.import_url, adapterId, savedSourceUrl);
  return <article className={`panel resume-source-card${record?.confirmed ? " is-confirmed" : ""}`} aria-labelledby={`resume-source-${adapterId}`}>
    <div className="resume-source-head"><div><span className="eyebrow">ИСТОЧНИК РЕЗЮМЕ</span><h2 id={`resume-source-${adapterId}`}>{site.label}</h2></div>{(hasSavedSource || checking) && !editing && <span className={`status ${isUnavailable ? "status-danger" : record?.status === "changed" ? "status-warning" : "status-success"}`}><span>{sourceStatusLabel(record, checking)}</span>{record?.status === "valid" && <small className="resume-source-confirmed">Подтверждено</small>}</span>}</div>
    {(!hasSavedSource || editing) && <>
      <label className="profile-full-field">Ссылка на резюме на {site.label}
        <input type="url" aria-label={`Ссылка на резюме на ${site.label}`} value={url} onChange={(event) => { setUrl(event.target.value); setError(""); }} placeholder={`https://${site.id === "hh" ? "hh.ru" : site.id === "hirehi" ? "hirehi.ru" : "zarplata.ru"}/resume/...`} autoComplete="off" />
      </label>
      <button type="button" className="secondary" onClick={() => previewMutation.mutate()} disabled={previewMutation.isPending || !url.trim()}>{previewMutation.isPending ? "Проверяем…" : "Проверить ссылку"}</button>
      {error && <Notice tone="danger" role="alert">{error}</Notice>}
    </>}
    {hasSavedSource && <>
      {usesSavedData && <div className="resume-source-address"><span>Готовность данных</span><small>{record?.resumeDataStatus === "ready" ? "Данные сохранены на этом компьютере" : record?.resumeDataStatus === "corrupt" ? "Сохранённые данные повреждены" : "Данные ещё не сохранены или требуют обновления"}</small>{record?.resumeDataSavedAt && <small>Сохранено: {formatServerUtc(record.resumeDataSavedAt)}</small>}</div>}
      {usesSavedData && <div className="resume-source-address"><span>Последнее обновление</span><small>{record?.status === "unavailable" ? "Не удалось обновить источник" : record?.checkedAt ? `Проверено: ${formatServerUtc(record.checkedAt)}` : "Ещё не обновлялось"}</small>{record?.resumeDataErrorMessage && <small>{record.resumeDataErrorMessage}</small>}</div>}
      <div className="resume-source-address" aria-label={`Сохранённая ссылка на резюме ${site.label}`}><span>Сохранённая ссылка</span>{savedSourceUrl ? <a href={savedSourceUrl} target="_blank" rel="noreferrer"><code>{savedAddress}</code></a> : <code>{savedAddress}</code>}</div>
      {savedImportUrl && <div className="resume-source-address resume-import-address" aria-label={`Ссылка для импорта ${site.label}`}><span>{adapterId === "hirehi" ? "Ссылка для импорта" : "Печатная версия резюме"}</span><small>{adapterId === "hirehi" ? "Эта ссылка используется для получения резюме с HireHi." : "Приложение получает данные резюме из его печатной версии."}</small><a href={savedImportUrl} target="_blank" rel="noreferrer"><code>{savedImportUrl}</code></a></div>}
    </>}
    {preview && <div className="resume-source-preview" aria-label={`Предпросмотр резюме ${site.label}`}>
      <div className="resume-source-summary"><strong>{resumeDisplayTitle(preview)}</strong></div>
      {editing && token && <button type="button" className="secondary" onClick={() => detailsMutation.mutate()} disabled={detailsMutation.isPending}>{detailsMutation.isPending ? "Загружаем подробности…" : "Показать все разделы и контакты"}</button>}
      {profileQuestions.length > 0 && <div className="resume-source-block resume-source-questions"><h3>Уточните о себе</h3>{profileQuestions.map((question) => <label className="resume-source-question" key={question.id}><span>{question.id === "grammatical_gender" ? "Ваш пол" : question.question}{question.required !== false && <em aria-hidden="true"> *</em>}</span>{question.options?.length ? <select aria-label={question.question} value={resumeAnswers[question.id] ?? ""} onChange={(event) => updateResumeAnswer(question.id, event.target.value)}><option value="">Выберите вариант</option>{question.options.map((option) => <option key={option} value={option}>{questionOptionLabel(question, option)}</option>)}</select> : <input aria-label={question.question} value={resumeAnswers[question.id] ?? ""} onChange={(event) => updateResumeAnswer(question.id, event.target.value)} placeholder="Короткий ответ" />}</label>)}</div>}
      {previewSections(preview).length > 0 && <div className="resume-source-block resume-coverage"><h3>Полнота резюме</h3><ul className="resume-source-list">{previewSections(preview).map((item) => <li key={`${item.label}-${item.status}`}><span>{item.label}</span><small>{statusLabel(item.status)}</small></li>)}</ul></div>}
      {preview.contacts && <div className="resume-source-block resume-coverage"><h3>Контакты</h3><p>{[...(preview.contacts.found ?? []).map(resumeContactLabel), ...(preview.contacts.hidden ?? []).map((item) => `${resumeContactLabel(item)} (скрыто)`)].join(", ") || "Контакты не найдены"}</p></div>}
      {(!record?.confirmed || editing) && <>
        <label className="checkline resume-source-consent"><input type="checkbox" checked={consent} onChange={(event) => setConsent(event.target.checked)} /> Это моё резюме. {usesSavedData ? "Разрешаю сохранить ссылку и данные резюме локально для использования в сессиях." : "Разрешаю сохранить ссылку и проверять актуальность резюме перед запуском сессий."}</label>
        {usesSavedData && <p>Для первого чтения и обновления откройте доступ к резюме по ссылке. После сохранения данных доступ можно закрыть.</p>}
        <button type="button" className="primary" onClick={confirm} disabled={!consent || confirmMutation.isPending || !profileQuestions.every((question) => isResumeQuestionValid(question, resumeAnswers[question.id] ?? ""))}>{confirmMutation.isPending ? "Сохраняем…" : "Подтвердить резюме"}</button>
      </>}
      {hasSavedSource && !editing && <div className="actions">{usesSavedData && <button type="button" className="secondary" onClick={() => refreshMutation.mutate()} disabled={refreshMutation.isPending}>{refreshMutation.isPending ? "Обновляем…" : "Обновить данные"}</button>}<button type="button" className="secondary" onClick={beginReplace}>Заменить источник</button><button type="button" className="danger" onClick={() => setDeletePending(true)}>Удалить источник</button><a className="button-link secondary" href={resumeEditUrl(adapterId)} target="_blank" rel="noreferrer">{site.editLabel}</a></div>}
    </div>}
    {hasSavedSource && !editing && !preview && <div className="actions">{usesSavedData && <button type="button" className="secondary" onClick={() => refreshMutation.mutate()} disabled={refreshMutation.isPending}>{refreshMutation.isPending ? "Обновляем…" : "Обновить данные"}</button>}<button type="button" className="secondary" onClick={beginReplace}>Заменить источник</button><button type="button" className="danger" onClick={() => setDeletePending(true)}>Удалить источник</button><a className="button-link secondary" href={resumeEditUrl(adapterId)} target="_blank" rel="noreferrer">{site.editLabel}</a></div>}
    {error && !editing && hasSavedSource && <Notice tone="danger" role="alert">{error}</Notice>}
    {deletePending && hasSavedSource && !editing && <div className="resume-source-delete-confirm" role="alert"><p>Удалить сохранённый источник {site.label}? Его можно будет добавить снова.</p><div className="actions"><button type="button" className="danger" onClick={remove} disabled={removeMutation.isPending}>{removeMutation.isPending ? "Удаляем…" : "Удалить"}</button><button type="button" className="secondary" onClick={() => setDeletePending(false)}>Отмена</button></div></div>}
    {hasSavedSource && editing && !preview && <button type="button" className="secondary" onClick={cancelReplace}>Отмена замены</button>}
  </article>;
}

function ResumeSourcesPage() {
  const qc = useQueryClient();
  const sourcesQuery = useQuery({ queryKey: ["resume-sources"], queryFn: () => fetchResumeSources(), staleTime: 30_000, refetchInterval: false, refetchOnWindowFocus: false });
  const sources = sourcesQuery.data ?? {};
  const removeSource = (adapterId: string) => { const next = { ...sources }; delete next[adapterId]; qc.setQueryData(["resume-sources"], next); };
  const saveSource = (record: ResumeSourceRecord) => {
    const current = qc.getQueryData<Record<string, ResumeSourceRecord>>(["resume-sources"]) ?? {};
    qc.setQueryData(["resume-sources"], { ...current, [record.adapterId]: record });
  };
  const saveRefresh = (record: ResumeSourceRecord) => saveSource(record);
  const savePreference = async (adapterId: string, grammaticalGender: "male" | "female") => {
    const result = await api<unknown>(`/resume-sources/${adapterId}`, { method: "PATCH", body: JSON.stringify({ grammatical_gender: grammaticalGender }) });
    const updated = sourceRecordFromResponse(result);
    const current = qc.getQueryData<Record<string, ResumeSourceRecord>>(["resume-sources"]) ?? {};
    const existing = current[adapterId];
    const merged = updated
      ? { ...existing, ...updated, preview: Object.keys(updated.preview).length ? updated.preview : existing?.preview }
      : existing;
    qc.setQueryData(["resume-sources"], {
      ...current,
      [adapterId]: {
        ...merged,
        grammaticalGender: updated?.grammaticalGender ?? grammaticalGender,
      },
    });
  };
  return <section className="page profile-sources-page">
    <Title eyebrow="ИСТОЧНИКИ РЕЗЮМЕ" note="Добавьте ссылку на резюме и подтвердите её.">Профиль — ссылки на резюме</Title>
    <div className="resume-source-grid">{RESUME_SITES.map((site) => <ResumePreviewCard key={site.id} adapterId={site.id} record={sources[site.id]} checking={sourcesQuery.isLoading} onConfirmed={saveSource} onPreferenceChange={savePreference} onRemoved={removeSource} onRefreshed={saveRefresh} />)}</div>
  </section>;
}

function sessionItems(value: unknown): JobSession[] {
  if (Array.isArray(value)) return value as JobSession[];
  if (value && typeof value === "object" && Array.isArray((value as { items?: unknown }).items)) return (value as { items: JobSession[] }).items;
  return [];
}

function sessionHistoryPage(value: unknown): SessionHistoryPage {
  if (value && typeof value === "object" && Array.isArray((value as { items?: unknown }).items)) {
    const page = value as Partial<SessionHistoryPage> & { items: JobSession[] };
    const items = page.items.filter((session) => TERMINAL_SESSION_STATUSES.includes(session.status));
    const limit = typeof page.limit === "number" ? page.limit : 10;
    const offset = typeof page.offset === "number" ? page.offset : 0;
    const total = typeof page.total === "number" ? page.total : items.length;
    return { items, total, limit, offset, has_more: page.has_more === true || offset + items.length < total };
  }
  const items = sessionItems(value).filter((session) => TERMINAL_SESSION_STATUSES.includes(session.status));
  return { items, total: items.length, limit: items.length || 10, offset: 0, has_more: false };
}

function sessionFromCreate(value: unknown): JobSession | null {
  if (value && typeof value === "object" && value !== null && "session" in value) {
    const session = (value as { session?: unknown }).session;
    return session && typeof session === "object" && typeof (session as { id?: unknown }).id === "number" ? session as JobSession : null;
  }
  return value && typeof value === "object" && typeof (value as { id?: unknown }).id === "number" ? value as JobSession : null;
}

function createIntentKey(): string {
  const randomUuid = typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  return `session-create-${randomUuid}`;
}

const CREATED_SESSION_STORAGE_KEY = "job-orchestrator.last-created-session";
const SEEN_FAILURES_STORAGE_KEY = "job-orchestrator.seen-session-failures";

function storedCreatedSessionId(): number | null {
  try {
    const value = Number(localStorage.getItem(CREATED_SESSION_STORAGE_KEY));
    return Number.isSafeInteger(value) && value > 0 ? value : null;
  } catch { return null; }
}

function storedSeenFailureIds(): number[] {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(SEEN_FAILURES_STORAGE_KEY) || "[]");
    return Array.isArray(value) ? value.filter((id): id is number => Number.isSafeInteger(id) && id > 0) : [];
  } catch { return []; }
}

function sessionFailureReason(session: JobSession): string {
  const details = session as JobSession & { failure_reason?: unknown; error_message?: unknown };
  const reason = [details.failure_reason, details.error_message, session.stop_reason]
    .find((value): value is string => typeof value === "string" && Boolean(value.trim()));
  return sessionReasonText(reason?.trim()) || "Причина сбоя не указана.";
}

function lifecycleNotice(session: JobSession): { tone: "info" | "success" | "warning" | "danger"; text: string } {
  const prefix = `Сессия #${session.id}`;
  switch (session.status) {
    case "CREATED": return { tone: "info", text: `${prefix} создана и ожидает запуска.` };
    case "PREPARING": return { tone: "info", text: `${prefix} готовится к запуску.` };
    case "RUNNING": return { tone: "success", text: `${prefix} запущена.` };
    case "PAUSED": return { tone: "warning", text: `${prefix} приостановлена. ${pausedSessionMessage(session.stop_reason)}` };
    case "STOPPING": return { tone: "warning", text: `${prefix}: остановка запрошена, ожидаем завершения.` };
    case "STOPPED": return { tone: "info", text: `${prefix} остановлена.` };
    case "COMPLETED": return { tone: "success", text: `${prefix} завершена.` };
    case "CANCELLED": return { tone: "info", text: `${prefix} отменена.` };
    case "FAILED": return { tone: "danger", text: `${prefix} завершилась с ошибкой: ${sessionFailureReason(session)}` };
    default: return { tone: "info", text: `${prefix}: состояние ${humanStatus(session.status)}.` };
  }
}

function SessionPage() {
  const [guaranteedApplication, setGuaranteedApplication] = useState(false);
  const [coverLetterOpen, setCoverLetterOpen] = useState(false);
  const [influenceOpen, setInfluenceOpen] = useState(false);
  const qc = useQueryClient();
  const resumeSourcesQuery = useQuery({
    queryKey: ["resume-sources"],
    queryFn: () => fetchResumeSources(),
    staleTime: 30_000,
    refetchInterval: false,
    refetchOnWindowFocus: false,
  });
  const resumeSources = resumeSourcesQuery.data ?? {};
  const sessions = useQuery({
    queryKey: ["sessions"],
    queryFn: async () => sessionItems(await api<unknown>("/sessions", { cache: "no-store" })),
    refetchInterval: 2000,
  });
  const { draft, updateDraft, status: draftStatus, conflict: draftConflict, loadSaved } = useSessionDraft();
  const { adapter, applicationLimit, desiredJobDescription, coverLetterAuto, coverLetterTemplate, coverLetterMaxWords, unlimitedApplications, hirehiProEnabled, influence } = draft;
  const resumeSource = resumeSources[adapter];
  // A saved source is durable profile data. Its latest availability and any
  // launch-only validation token must never decide whether the source is
  // shown or whether the user may try to launch again.
  const genderPreferenceRequired = Boolean(resumeSource && resumeQuestions(resumeSource.preview).some((question) => question.id === "grammatical_gender" && question.required !== false));
  const missingGenderPreference = Boolean(genderPreferenceRequired && resumeSource?.grammaticalGender == null);
  const savedDataRequired = adapter === "hh" || adapter === "zarplata";
  const resumeDataReady = !savedDataRequired || resumeSource?.resumeDataStatus === "ready";
  const profileReady = Boolean(resumeSource && resumeDataReady && !missingGenderPreference);
  const profileUnavailable = resumeSource?.status === "unavailable";
  const resumeDataBlocked = Boolean(savedDataRequired && resumeSource && !resumeDataReady);
  const resumeSourceUrl = resumeSource
    ? publicResumeSourceUrl(resumeSource.sourceUrl, adapter)
      ?? publicResumeSourceUrl(resumeSource.preview.source_url, adapter)
    : undefined;
  const resumeAddress = resumeSource
    ? resumeSourceUrl
      ?? (resumeSource.maskedUrl || resumeSource.preview.masked_url
        ? safeResumeUrlLabel(resumeSource.maskedUrl ?? resumeSource.preview.masked_url)
        : "Ссылка сохранена")
    : "";
  const desiredJobDescriptionRef = useRef<HTMLTextAreaElement>(null);
  const idempotencyKeyRef = useRef<string | null>(null);
  const createInFlightRef = useRef(false);
  const [pendingActions, setPendingActions] = useState<Record<string, boolean>>({});
  const [createdSessionId, setCreatedSessionId] = useState<number | null>(() => storedCreatedSessionId());
  const [createdSessionSnapshot, setCreatedSessionSnapshot] = useState<JobSession | null>(null);
  const [seenFailureIds, setSeenFailureIds] = useState<number[]>(() => storedSeenFailureIds());
  const [historyOpen, setHistoryOpen] = useState(false);
  const terminalSessionSignature = useRef("");
  const [historyOffset, setHistoryOffset] = useState(0);
  const history = useQuery({
    queryKey: ["session-history", historyOffset],
    queryFn: async () => sessionHistoryPage(await api<unknown>(`/sessions/history?terminal_only=true&limit=10&offset=${historyOffset}`, { cache: "no-store" })),
    placeholderData: (previous) => previous,
    staleTime: 5_000,
    refetchOnWindowFocus: false,
  });
  const resizeDesiredJobDescription = useCallback(() => {
    const textarea = desiredJobDescriptionRef.current;
    if (!textarea) return;
    textarea.style.height = "auto";
    const styles = window.getComputedStyle(textarea);
    const borderHeight = Number.parseFloat(styles.borderTopWidth || "0") + Number.parseFloat(styles.borderBottomWidth || "0");
    textarea.style.height = `${textarea.scrollHeight + (styles.boxSizing === "border-box" ? borderHeight : 0)}px`;
  }, []);
  useLayoutEffect(() => { resizeDesiredJobDescription(); }, [desiredJobDescription, resizeDesiredJobDescription]);
  const blockedByAdapter = (sessions.data ?? []).some((session) => session.adapter_id === adapter && !TERMINAL_SESSION_STATUSES.includes(session.status));
  const [message, setMessage] = useState("");
  const [messageTone, setMessageTone] = useState<"neutral" | "success" | "warning" | "danger" | "info">("neutral");
  const validLimit = (value: string, unlimited: boolean) =>
    unlimited || /^[1-9]\d*$/.test(value);
  // HireHi searches are intentionally open-ended. The user controls their
  // lifetime explicitly with the session Stop action, rather than a form
  // limit; HH/Zarplata retain their saved per-session limit settings.
  const limitsAreValid = adapter === "hirehi" || validLimit(applicationLimit, unlimitedApplications);
  const coverLetterIsValid = coverLetterAuto || coverLetterTemplate.trim().length > 0;
  const coverLetterMaxWordsValue = coverLetterMaxWords ?? "";
  const coverLetterMaxWordsAreValid = coverLetterMaxWordsValue === "" || (/^[1-9]\d*$/.test(coverLetterMaxWordsValue) && Number(coverLetterMaxWordsValue) <= 10000);
  const create = useMutation({
    mutationFn: async () => {
      // HH/Zarplata launches use the durable local copy; HireHi revalidates
      // its source as part of this create request. Never add a separate
      // browser-token lifecycle or source refetch here.
      if (!idempotencyKeyRef.current) idempotencyKeyRef.current = createIntentKey();
      const idempotencyKey = idempotencyKeyRef.current;
      const response = await api<unknown>("/sessions", {
        method: "POST",
        headers: { "Idempotency-Key": idempotencyKey },
        body: JSON.stringify({
          adapter_id: adapter,
          hirehi_pro_enabled: adapter === "hirehi" ? hirehiProEnabled : false,
          guaranteed_application: guaranteedApplication,
          application_limit: adapter === "hirehi" ? null : unlimitedApplications ? null : Number(applicationLimit),
          desired_job_description: desiredJobDescription.trim(),
          cover_letter_auto: coverLetterAuto,
          cover_letter_template: coverLetterTemplate,
          cover_letter_max_words: coverLetterMaxWordsValue.trim() ? Number(coverLetterMaxWordsValue) : null,
          auto_start: true,
          minimum_scores: { ...Object.fromEntries(Object.entries(influence).map(([key, level]) => [key, INFLUENCE_LEVELS.indexOf(level) + 1])), special_requirements: 1 },
        }),
      });
      const session = sessionFromCreate(response);
      if (!session) throw new Error("Сервер не вернул идентификатор сессии");
      return session;
    },
    // A transport retry keeps the same Idempotency-Key from the ref above.
    // The runtime can therefore safely accept a repeated POST after a lost
    // response without creating a second session.
    retry: 1,
    onSuccess: (session) => {
      setCreatedSessionId(session.id);
      setCreatedSessionSnapshot(session);
      try { localStorage.setItem(CREATED_SESSION_STORAGE_KEY, String(session.id)); } catch { /* storage can be unavailable */ }
      setMessage("");
      toast.success(`Сессия #${session.id} принята`);
      idempotencyKeyRef.current = null;
      createInFlightRef.current = false;
      void qc.invalidateQueries({ queryKey: ["sessions"] });
      void qc.invalidateQueries({ queryKey: ["session-report"] });
      void qc.invalidateQueries({ queryKey: ["session-history"] });
    },
    onError: (error) => { const message = error instanceof Error ? error.message : "Не удалось выполнить действие"; setMessageTone("danger"); setMessage(message); toast.error(message); },
  });
  const launchSession = () => {
    if (createInFlightRef.current || create.isPending) return;
    createInFlightRef.current = true;
    create.mutate();
  };
  useEffect(() => {
    if (create.isError) {
      idempotencyKeyRef.current = null;
      createInFlightRef.current = false;
    }
  }, [create.isError]);
  const action = async (sessionId: number, name: string) => {
    const actionKey = `${sessionId}:${name}`;
    setPendingActions((current) => ({ ...current, [actionKey]: true }));
    try {
      const response = await api<{ message?: string }>(
        `/sessions/${sessionId}/${name}`,
        { method: "POST" },
      );
      const actionMessage = response.message || (name === "stop"
        ? "Запрос на остановку принят. Сессия завершится после остановки текущего шага."
        : name === "start" ? "Запрос на запуск принят." : name === "resume" ? "Запрос на продолжение принят." : "Состояние сессии обновлено.");
      setMessageTone(name === "stop" ? "info" : "success");
      setMessage(actionMessage);
      if (name === "stop") toast.info(actionMessage); else toast.success(actionMessage);
      await qc.invalidateQueries({ queryKey: ["sessions"] });
      await qc.invalidateQueries({ queryKey: ["session-report"] });
      await qc.invalidateQueries({ queryKey: ["session-history"] });
    } catch (error) {
      const message = error instanceof Error ? error.message : "Не удалось выполнить действие"; setMessageTone("danger"); setMessage(message); toast.error(message);
    } finally {
      setPendingActions((current) => {
        const next = { ...current };
        delete next[actionKey];
        return next;
      });
    }
  };
  const formatSessionLimit = (limit: number | null | undefined): string =>
    limit === null ? "без ограничений" : String(limit ?? "—");
  const terminalHistoryIds = new Set((history.data?.items ?? []).filter((session) => TERMINAL_SESSION_STATUSES.includes(session.status)).map((session) => session.id));
  const visibleSessions = (sessions.data ?? []).filter((session, index, items) => !TERMINAL_SESSION_STATUSES.includes(session.status) && !terminalHistoryIds.has(session.id) && items.findIndex((candidate) => candidate.id === session.id) === index);
  const activeIds = new Set(visibleSessions.map((session) => session.id));
  const historyItems = (history.data?.items ?? []).filter((session, index, items) => TERMINAL_SESSION_STATUSES.includes(session.status) && !activeIds.has(session.id) && items.findIndex((candidate) => candidate.id === session.id) === index);
  const allKnownSessions = new Map<number, JobSession>();
  (sessions.data ?? []).forEach((session) => allKnownSessions.set(session.id, session));
  (history.data?.items ?? []).filter((session) => TERMINAL_SESSION_STATUSES.includes(session.status)).forEach((session) => {
    const current = allKnownSessions.get(session.id);
    if (!current || !TERMINAL_SESSION_STATUSES.includes(current.status)) allKnownSessions.set(session.id, session);
  });
  const createdSession = createdSessionId === null ? null : allKnownSessions.get(createdSessionId) ?? (createdSessionSnapshot?.id === createdSessionId ? createdSessionSnapshot : null);
  const createdLifecycle = createdSession ? lifecycleNotice(createdSession) : null;
  const latestTerminalByAdapter = new Map<string, JobSession>();
  [...allKnownSessions.values()].filter((session) => TERMINAL_SESSION_STATUSES.includes(session.status)).forEach((session) => {
    const previous = latestTerminalByAdapter.get(session.adapter_id);
    const sessionTime = Date.parse(session.finished_at || "");
    const previousTime = previous ? Date.parse(previous.finished_at || "") : Number.NaN;
    if (!previous || (Number.isFinite(sessionTime) && (!Number.isFinite(previousTime) || sessionTime > previousTime)) || ((!Number.isFinite(sessionTime) || !Number.isFinite(previousTime)) && session.id > previous.id)) {
      latestTerminalByAdapter.set(session.adapter_id, session);
    }
  });
  const visibleFailures = [...latestTerminalByAdapter.values()]
    .filter((session) => session.status === "FAILED" && !seenFailureIds.includes(session.id))
    .sort((left, right) => left.id - right.id);
  useEffect(() => {
    const terminal = (sessions.data ?? []).filter((session) => TERMINAL_SESSION_STATUSES.includes(session.status));
    const signature = terminal.map((session) => `${session.id}:${session.status}`).sort().join(",");
    if (signature && signature !== terminalSessionSignature.current) {
      terminalSessionSignature.current = signature;
      void qc.invalidateQueries({ queryKey: ["session-history"] });
    }
  }, [sessions.data, qc]);
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
          <section className="subsection form-rail session-main-info" aria-labelledby="session-main-info-heading">
            <div className="section-heading"><h3 id="session-main-info-heading">Основная информация</h3></div>
            <div className="row session-top-row">
              <SingleSelect label="Сайт" options={RESUME_SITES.map((site) => [site.id, site.label] as const)} value={adapter} onValueChange={(nextAdapter) => updateDraft({ adapter: nextAdapter, hirehiProEnabled: nextAdapter === "hirehi" ? hirehiProEnabled : false })} />
              {adapter === "hirehi" ? <div className="session-limit-readonly" aria-live="polite">
                <strong>Лимит вакансий в работе</strong>
                <small>Поиск работает без лимита и продолжается до ручной остановки.</small>
              </div> : <label>
                Лимит вакансий в работе
                <input aria-label="Лимит вакансий в работе" type="number" min="1" step="1" value={applicationLimit} disabled={unlimitedApplications} onChange={(e) => updateDraft({ applicationLimit: e.target.value })} />
                <small>Считаются отклики, подтверждённые выбранной площадкой.</small>
                <span className="checkline"><input aria-label="Без ограничений: отправка откликов" type="checkbox" checked={unlimitedApplications} onChange={(e) => updateDraft({ unlimitedApplications: e.target.checked })} />Без ограничений</span>
              </label>}
            </div>
            <div className={`session-resume-requirement${profileReady ? " is-ready" : ""}`} aria-live="polite">
              <div><strong>Резюме для {resumeSite(adapter).label}</strong>{resumeSource ? <><code className="session-resume-address">{resumeAddress}</code>{savedDataRequired && resumeDataReady && <span>Сессия использует сохранённые данные резюме.</span>}{profileUnavailable && !savedDataRequired && <span>Источник сохранён; актуальность будет проверена при запуске.</span>}{resumeDataBlocked && <span>Обновите данные резюме во вкладке «Профиль».</span>}{missingGenderPreference && <span>Заполните данные резюме в профиле.</span>}</> : <span>Нужно добавить и подтвердить ссылку на этой площадке.</span>}</div>
              <NavLink className="button-link secondary" to="/profile">{resumeDataBlocked ? "Обновить в профиле" : resumeSource ? "Открыть источник" : "Добавить ссылку в профиле"}</NavLink>
            </div>
            <label className="profile-full-field session-description-field">
              Описание желаемой вакансии
              <textarea
                ref={desiredJobDescriptionRef}
                className="session-description-textarea"
                aria-label="Описание желаемой вакансии"
                maxLength={2000}
                rows={5}
                value={desiredJobDescription}
                onChange={(event) => { updateDraft({ desiredJobDescription: event.target.value }); resizeDesiredJobDescription(); }}
                placeholder="Опишите желательные и нежелательные факторы вакансии"
              />
              <small>{desiredJobDescription.length} / 2000 символов</small>
              <small role="status">{draftStatus}</small>
              {draftConflict && <button type="button" className="secondary" onClick={() => void loadSaved()}>Заменить форму сохранённой копией</button>}
            </label>
            <div className="guaranteed-mode">
              <label className="checkline"><input type="checkbox" aria-label="Гарантированный отклик" checked={guaranteedApplication} onChange={(event) => setGuaranteedApplication(event.target.checked)} />Гарантированный отклик</label>
              <small>ИИ сможет дополнять ответы правдоподобными сведениями, которых нет в резюме. Режим не гарантирует отправку отклика или оффер.</small>
            </div>
            {adapter === "hirehi" && <div className="hirehi-pro-option">
              <label className="checkline"><input type="checkbox" aria-label="У меня есть подписка PRO" checked={hirehiProEnabled} onChange={(event) => updateDraft({ hirehiProEnabled: event.target.checked })} />У меня есть подписка PRO</label>
              <small>PRO-инструменты используются только если подписка уже есть; приложение ничего не покупает.</small>
            </div>}
          </section>
          <section className={`subsection form-rail session-collapsible cover-letter-settings${coverLetterOpen ? " is-open" : ""}`} aria-labelledby="cover-letter-heading">
            <h3 id="cover-letter-heading" className="session-collapsible-heading"><button type="button" className="session-collapsible-trigger" aria-expanded={coverLetterOpen} aria-controls="cover-letter-fields" onClick={() => setCoverLetterOpen((open) => !open)}><span>Сопроводительное письмо</span><ChevronIcon className="session-collapsible-icon" /></button></h3>
            <div id="cover-letter-fields" className="session-collapsible-content" hidden={!coverLetterOpen}>
              <label className="checkline"><input type="checkbox" aria-label="ИИ самостоятельно определяет структуру сопроводительного письма" checked={coverLetterAuto} onChange={(event) => updateDraft({ coverLetterAuto: event.target.checked })} />ИИ самостоятельно определяет структуру письма</label>
              {!coverLetterAuto && <>
                <label className="field-prose">Своя структура сопроводительного письма
                  <textarea aria-label="Своя структура сопроводительного письма" maxLength={12000} rows={8} value={coverLetterTemplate} onChange={(event) => updateDraft({ coverLetterTemplate: event.target.value })} placeholder="Например: Я [ФИО] — ..." />
                  <small>{coverLetterTemplate.length} / 12000 символов. Особые требования работодателя будут выполнены в любом режиме.</small>
                </label>
                <label className="field-compact">Максимальная длина письма, слов
                  <input aria-label="Максимальная длина сопроводительного письма в словах" type="number" min="1" max="10000" step="1" maxLength={32} value={coverLetterMaxWordsValue} onChange={(event) => updateDraft({ coverLetterMaxWords: event.target.value.slice(0, 32) })} placeholder="По умолчанию: 150" />
                </label>
              </>}
            </div>
          </section>
          <section className={`influence-section session-collapsible${influenceOpen ? " is-open" : ""}`} aria-labelledby="influence-heading">
            <h3 id="influence-heading" className="session-collapsible-heading"><button type="button" className="session-collapsible-trigger" aria-expanded={influenceOpen} aria-controls="influence-fields" onClick={() => setInfluenceOpen((open) => !open)}><span>Влияние факторов на вакансии</span><ChevronIcon className="session-collapsible-icon" /></button></h3>
            <div id="influence-fields" className="session-collapsible-content" hidden={!influenceOpen}>
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
            </div>
          </section>
          <button type="button" className="primary"
            onClick={launchSession}
            disabled={!profileReady || create.isPending || !limitsAreValid || !coverLetterIsValid || !coverLetterMaxWordsAreValid || blockedByAdapter}
          >
            {create.isPending ? "Запускаем…" : "Создать и запустить"}
          </button>
          {blockedByAdapter && <Notice tone="warning" role="alert">Для выбранного сайта уже есть незавершённая сессия. Дождитесь её завершения.</Notice>}
          {!limitsAreValid && (
            <Notice tone="danger" role="alert">
              Введите целое положительное значение лимита или включите «Без ограничений».
            </Notice>
          )}
          {!coverLetterIsValid && (
            <Notice tone="danger" role="alert">Добавьте структуру сопроводительного письма или включите автоматическую структуру.</Notice>
          )}
          {!coverLetterMaxWordsAreValid && (
            <Notice tone="danger" role="alert">Укажите целое число от 1 до 10000 слов или оставьте поле пустым.</Notice>
          )}
          {!profileReady && !resumeDataBlocked && !profileUnavailable && !missingGenderPreference && (
            <Notice tone="warning">
              <>Для запуска нужно проверить и подтвердить резюме на сайте {resumeSite(adapter).label}. <NavLink className="button-link secondary" to="/profile">Открыть профиль</NavLink></>
            </Notice>
          )}
          {resumeDataBlocked && <Notice tone="warning">Обновите данные резюме во вкладке «Профиль». <NavLink className="button-link secondary" to="/profile">Открыть профиль</NavLink></Notice>}
          {missingGenderPreference && <Notice tone="warning">Заполните данные резюме в профиле перед запуском сессии. <NavLink className="button-link secondary" to="/profile">Открыть профиль</NavLink></Notice>}
        </article>
      {createdLifecycle && createdSession?.status !== "FAILED" && <Notice tone={createdLifecycle.tone} role={createdLifecycle.tone === "danger" ? "alert" : "status"}>{createdLifecycle.text}</Notice>}
      {visibleFailures.map((failure) => <Notice key={failure.id} tone="danger" role="alert">
        <span><strong>Сессия #{failure.id} завершилась с ошибкой на площадке {resumeSite(failure.adapter_id).label}.</strong> {sessionFailureReason(failure)}</span>
        <span className="session-failure-actions">
          <button type="button" className="secondary" onClick={() => setHistoryOpen(true)}>Открыть историю сессий</button>
          <button type="button" className="secondary" onClick={() => {
            const next = [...new Set([...seenFailureIds, failure.id])];
            setSeenFailureIds(next);
            try { localStorage.setItem(SEEN_FAILURES_STORAGE_KEY, JSON.stringify(next)); } catch { /* storage can be unavailable */ }
          }}>Скрыть сообщение</button>
        </span>
      </Notice>)}
      {message && !(visibleFailures.length > 0 && messageTone === "success") && <Notice tone={messageTone}>{message}</Notice>}
      {visibleSessions.map((session) => {
        return <SessionCard key={session.id} session={session} formatSessionLimit={formatSessionLimit} action={action} pendingAction={Object.keys(pendingActions).find((key) => key.startsWith(`${session.id}:`))?.split(":")[1]} />;
      })}
      {history.data?.total ? <details className="session-history" open={historyOpen} onToggle={(event) => setHistoryOpen(event.currentTarget.open)} aria-labelledby="session-history-heading">
        <summary>
          <span className="session-history-head"><span><span className="eyebrow">ИСТОРИЯ</span><strong id="session-history-heading">Завершённые сессии ({history.data.total})</strong></span><span className="session-history-total">{history.data.total}</span></span>
        </summary>
        {history.error ? <p className="vacancy-error-message" role="alert">Не удалось загрузить историю сессий.</p> : historyItems.length === 0 ? <p className="empty-score">История пока пуста.</p> : <>
          {historyItems.map((session) => <SessionCard key={session.id} session={session} formatSessionLimit={formatSessionLimit} action={action} pendingAction={Object.keys(pendingActions).find((key) => key.startsWith(`${session.id}:`))?.split(":")[1]} />)}
          <div className="session-history-pagination"><button type="button" className="secondary" onClick={() => setHistoryOffset((value) => Math.max(0, value - 10))} disabled={historyOffset === 0 || history.isFetching}>Назад</button><span>Показаны {historyOffset + 1}–{Math.min(historyOffset + historyItems.length, history.data?.total ?? historyOffset + historyItems.length)}</span><button type="button" className="secondary" onClick={() => setHistoryOffset((value) => value + 10)} disabled={!history.data?.has_more || history.isFetching}>Дальше</button></div>
        </>}
      </details> : null}
    </section>
  );
}

function ResumeDetails({ session }: { session: JobSession }) {
  const [open, setOpen] = useState(false);
  const [aiOpen, setAiOpen] = useState(false);
  const snapshot = useQuery({
    queryKey: ["session-resume", session.id],
    queryFn: () => api<ResumeSnapshot>(`/sessions/${session.id}/resume`, { cache: "no-store" }),
    enabled: open,
    staleTime: 0,
    retry: false,
  });
  const aiContext = useQuery({
    queryKey: ["session-ai-context", session.id],
    queryFn: () => api<ResumeAiContext>(`/sessions/${session.id}/ai-context`, { cache: "no-store" }),
    enabled: aiOpen,
    staleTime: 0,
    retry: false,
  });
  const data = snapshot.data;
  const nestedResume = data?.resume && typeof data.resume === "object" ? data.resume as Record<string, unknown> : data?.snapshot && typeof data.snapshot === "object" ? data.snapshot : {};
  const sectionsValue = data?.sections ?? nestedResume.sections;
  const sections = Array.isArray(sectionsValue) ? sectionsValue as Array<Record<string, unknown> | string> : [];
  const contactsValue = data?.contacts ?? nestedResume.contacts;
  const contacts = contactsValue && typeof contactsValue === "object" ? Object.entries(contactsValue) : [];
  const importUrl = safeResumeImportUrl(typeof data?.import_url === "string" ? data.import_url : typeof nestedResume.import_url === "string" ? nestedResume.import_url : null, session.adapter_id);
  const coverage = data?.coverage ?? nestedResume.coverage;
  return <div className="session-resume-details">
    <button type="button" className="secondary" aria-expanded={open} onClick={() => setOpen((value) => !value)}>{open ? "Скрыть резюме" : "Показать резюме и полноту"}</button>
    {open && <div className="session-resume-panel">
      {snapshot.isLoading ? <p role="status">Загружаем снимок резюме…</p> : snapshot.error ? <p className="vacancy-error-message" role="alert">Не удалось загрузить резюме сессии.</p> : data ? <>
        <div className="resume-source-block"><h3>Разделы</h3>{sections.length ? <ul className="resume-source-list">{sections.map((section, index) => <li key={index}><span>{typeof section === "string" ? section : String(section.title ?? section.label ?? section.key ?? "Раздел")}</span><small>{typeof section === "string" ? "Найдено" : String(section.status ?? "Найдено")}</small></li>)}</ul> : <p className="empty-score">Разделы не переданы.</p>}</div>
        <div className="resume-source-block"><h3>Контакты</h3>{contacts.length ? <ul className="resume-source-list">{contacts.map(([key, value]) => <li key={key}><span>{key}</span><small>{typeof value === "string" ? value : JSON.stringify(value)}</small></li>)}</ul> : <p className="empty-score">Контакты не переданы.</p>}</div>
        {coverage && <div className="resume-source-block"><h3>Полнота</h3><pre className="resume-json">{JSON.stringify(coverage, null, 2)}</pre></div>}
        {importUrl && <a className="button-link secondary" href={importUrl} target="_blank" rel="noreferrer">Печатный HTML</a>}
        <button type="button" className="secondary" aria-expanded={aiOpen} onClick={() => setAiOpen((value) => !value)}>{aiOpen ? "Скрыть контекст ИИ" : "Показать контекст ИИ"}</button>
        {aiOpen && (aiContext.isLoading ? <p role="status">Загружаем контекст…</p> : aiContext.error ? <p className="vacancy-error-message" role="alert">Не удалось загрузить контекст ИИ.</p> : <pre className="resume-json">{JSON.stringify(aiContext.data?.resume_context ?? aiContext.data?.context ?? aiContext.data, null, 2)}</pre>)}
      </> : null}
    </div>}
  </div>;
}

function HireHiReport({ session }: { session: JobSession }) {
  const report = useQuery({
    queryKey: ["session-report", session.id],
    queryFn: () => api<{ ready?: boolean; pdf_url?: string | null }>(`/sessions/${session.id}/report`, { cache: "no-store" }),
    enabled: session.adapter_id === "hirehi" && TERMINAL_SESSION_STATUSES.includes(session.status),
    staleTime: 30_000,
    retry: false,
  });
  const pdfUrl = report.data?.ready && report.data.pdf_url ? report.data.pdf_url : null;
  if (!pdfUrl) return null;
  return <a className="button-link session-report-button" href={pdfUrl} target="_blank" rel="noreferrer" aria-label={`Скачать PDF HireHi #${session.id}`}>
    <svg className="session-report-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M6 3h9l3 3v15H6z" /><path d="M9 14h6M9 17h4" /></svg>
    Скачать PDF-отчёт
  </a>;
}

function SessionCard({ session, formatSessionLimit, action, pendingAction }: { session: JobSession; formatSessionLimit: (limit: number | null | undefined) => string; action: (id: number, name: string) => Promise<void>; pendingAction?: string }) {
  const terminal = TERMINAL_SESSION_STATUSES.includes(session.status);
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
              {session.stage && <p className="session-stage" data-testid={`session-stage-${session.id}`}><strong>{"\u042d\u0442\u0430\u043f"}:</strong> {humanStatus(session.stage)}{formatSessionDuration(session) && <span> · {formatSessionDuration(session)}</span>}{session.wait_reason && <span> · {safeSessionText(session.wait_reason)}</span>}{session.next_retry_at && <span> · {"\u041f\u043e\u0432\u0442\u043e\u0440"}: {formatServerUtc(session.next_retry_at)}</span>}</p>}
              <p>
                {session.status === "PAUSED"
                  ? pausedSessionMessage(session.stop_reason)
                  : sessionReasonText(session.stop_reason) || "Обработка вакансий идёт последовательно"}
              </p>
              <p className="session-limits-summary">
                Лимит сессии: {session.adapter_id === "hirehi" ? "выбрано" : "отправка"} — {formatSessionLimit(session.application_limit)}.
              </p>
            </div>
            <div className="actions session-actions">
              {session.status === "CREATED" && (
                <>
                  <button type="button" className="primary" onClick={() => void action(session.id, "start")}>Запустить</button>
                  {browserAvailable && (
                    <button type="button" className="secondary" onClick={() => void action(session.id, "browser")}>Открыть браузер</button>
                  )}
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
              {session.status === "PREPARING" && <button type="button" className="danger" onClick={() => void action(session.id, "stop")} disabled={pendingAction === "stop"}>{"\u041e\u0441\u0442\u0430\u043d\u043e\u0432\u0438\u0442\u044c"}</button>}
              {session.status === "STOPPING" && <button type="button" className="danger" disabled title={"\u041e\u0441\u0442\u0430\u043d\u043e\u0432\u043a\u0430 \u0443\u0436\u0435 \u0437\u0430\u043f\u0440\u043e\u0448\u0435\u043d\u0430"}>{"\u041e\u0441\u0442\u0430\u043d\u043e\u0432\u043a\u0430 \u0437\u0430\u043f\u0440\u043e\u0448\u0435\u043d\u0430"}</button>}
              {session.status === "PAUSED" && (
                <>
                  <button type="button" className="primary" onClick={() => void action(session.id, "resume")}>Продолжить</button>
                  <button type="button" className="danger" onClick={() => void action(session.id, "stop")} disabled={pendingAction === "stop"}>Остановить</button>
                </>
              )}
            </div>
            {terminal && <ResumeDetails session={session} />}
            {terminal && <HireHiReport session={session} />}
          </article>
          <div className="stats compact">
            {[
              ["Просмотрено", "viewed"],
              ["Отфильтровано", "filtered"],
              [session.adapter_id === "hirehi" ? "В отчёте" : "Отправлено", session.adapter_id === "hirehi" ? "reported" : "submitted"],
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
  return formatUtcTimestampLocal(value);
}

function VacancyCard({ v: rawVacancy }: { v: Vacancy }) {
  const [open, setOpen] = useState(false);
  const normalizedVacancy = detailedVacancy(rawVacancy, rawVacancy);
  const hasFullEvaluation = Boolean(normalizedVacancy.evaluation && (Array.isArray(normalizedVacancy.evaluation.score_breakdown) || normalizedVacancy.evaluation.reason));
  const legacySubmissionUnconfirmed = vacancyStatusGroup(normalizedVacancy) === "ERROR" && vacancyErrorCode(normalizedVacancy) === "SUBMISSION_UNCONFIRMED";
  const detail = useQuery({
    queryKey: ["vacancy-detail", rawVacancy.id],
    queryFn: () => api<Vacancy>(`/vacancies/${rawVacancy.id}`),
    // The list payload already contains the contractual legacy error. Keep
    // it visible while expanding instead of showing a lazy-detail spinner.
    enabled: open && !hasFullEvaluation && !legacySubmissionUnconfirmed,
    staleTime: 60_000,
    retry: false,
  });
  const fetchedDetail = detail.data && !Array.isArray(detail.data) ? detail.data : undefined;
  const displayed = fetchedDetail ? detailedVacancy(normalizedVacancy, fetchedDetail) : normalizedVacancy;
  const evaluation = displayed.evaluation;
  const breakdown = evaluationRows(evaluation);
  const v = { ...displayed, evaluation: evaluation as NonNullable<Vacancy["evaluation"]> };
  const statusGroup = vacancyStatusGroup(v);
  const errorMessage = vacancyErrorMessage(v);
  return <details className="panel vacancy-score vacancy-disclosure" open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary aria-expanded={open} aria-controls={`vacancy-details-${v.id}`}>
      <span className="vacancy-main"><b>{v.title}</b><small>#{v.id} · {v.company || "Компания не указана"}{v.site ? ` · ${v.site}` : ""}</small></span>
      <span className="score-total"><strong>{v.evaluation?.score ?? "—"}</strong><small>/ 100</small></span>
      <span><Status value={statusGroup} label={vacancyStatusLabel(v)} />{v.status_changed_at && <small className="vacancy-status-date">{formatStatusDate(v.status_changed_at)}</small>}</span>
      <span className="vacancy-disclosure-control"><span className="sr-only">{open ? "Скрыть подробности вакансии" : "Показать подробности вакансии"}</span><ChevronIcon className="vacancy-chevron" /></span>
    </summary>
    {detail.isLoading && !legacySubmissionUnconfirmed && statusGroup !== "ERROR" ? <p className="empty-score" role="status">Загрузка подробностей…</p> : detail.error && statusGroup !== "ERROR" ? <p className="vacancy-error-message" role="alert">Не удалось загрузить подробности вакансии.</p> : (hasFullEvaluation || detail.data) && evaluation ? <div className="score-details" id={`vacancy-details-${v.id}`}>
      <div className="resume-score-heading"><span className="eyebrow">КРАТКОЕ РЕЗЮМЕ</span></div>
      <p>{humanModelSummary(evaluation.reason || "", statusGroup, breakdown)}</p>
      {statusGroup === "ERROR" ? <><p className="vacancy-error-message">{errorMessage}</p><p className="vacancy-error-code">Код ошибки: {vacancyErrorCode(v)}</p></> : <p>{vacancyOutcome(v)}</p>}
      <div className="resume-score-heading"><span className="eyebrow">ПО КРИТЕРИЯМ</span></div>
      {presentationBreakdown(v.evaluation.score_breakdown ?? []).map((row) => <div className="score-row" key={row.key}><div><b>{row.title}</b><span>{row.max_points > 0 ? `${row.points} / ${row.max_points}` : "не применяется"}</span></div>{row.max_points > 0 && <div className="scorebar" role="progressbar" aria-label={`Релевантность: ${row.title}`} aria-valuenow={row.points} aria-valuemin={0} aria-valuemax={row.max_points}><i style={{ width: `${Math.min(100, Math.max(0, (row.points / row.max_points) * 100))}%` }} /></div>}<small>{humanCriterionExplanation(row.explanation)}</small></div>)}
      <a href={v.url} target="_blank" rel="noreferrer">Открыть вакансию на площадке</a>
    </div> : statusGroup === "ERROR"
      ? <div className="score-details" id={`vacancy-details-${v.id}`}><div className="resume-score-heading"><span className="eyebrow">КРАТКОЕ РЕЗЮМЕ</span><small>Что произошло с вакансией</small></div><p className="vacancy-error-message">{errorMessage}</p><p className="vacancy-error-code">Код ошибки: {vacancyErrorCode(v)}</p><a href={v.url} target="_blank" rel="noreferrer">Открыть вакансию на площадке</a></div>
      : <p className="empty-score">Оценка ещё не завершена.</p>}
  </details>;
}

function VacanciesPage() {
  const [offset, setOffset] = useState(0);
  const [allVacancies, setAllVacancies] = useState<Vacancy[]>([]);
  const [filters, setFilters] = useState<VacancyFilters>(DEFAULT_VACANCY_FILTERS);
  const [appliedFilters, setAppliedFilters] = useState<VacancyFilters>(DEFAULT_VACANCY_FILTERS);
  const [criteriaOpen, setCriteriaOpen] = useState(false);
  const filterErrors = validateVacancyFilters(filters);
  const hasFilterErrors = Object.keys(filterErrors).length > 0;
  const filterError = (field: keyof VacancyFilterRangeFields) => {
    const message = filterErrors[field];
    return message ? <small className="vacancy-error-message" id={`vacancy-filter-error-${field}`}>{message}</small> : null;
  };
  const filterInputProps = (field: keyof VacancyFilterRangeFields) => ({
    "aria-invalid": Boolean(filterErrors[field]),
    "aria-describedby": filterErrors[field] ? `vacancy-filter-error-${field}` : undefined,
  });
  const hasAppliedFilterErrors = Object.keys(validateVacancyFilters(appliedFilters)).length > 0;
  const appliedFilterKey = JSON.stringify(appliedFilters);
  const [allVacanciesKey, setAllVacanciesKey] = useState(appliedFilterKey);
  useEffect(() => {
    if (hasFilterErrors) return;
    const timer = window.setTimeout(() => setAppliedFilters(filters), 250);
    return () => window.clearTimeout(timer);
  }, [filters, hasFilterErrors]);
  // Keep placeholder data for same-key refreshes, but clear the accumulated
  // list as soon as the applied filter generation changes.
  useEffect(() => {
    setAllVacanciesKey(appliedFilterKey);
    setAllVacancies([]);
    setOffset(0);
  }, [appliedFilterKey]);
  const setFilter = (key: keyof VacancyFilters, value: string) => {
    setOffset(0);
    setFilters((current) => ({ ...current, [key]: value }));
  };
  const q = useQuery({
    queryKey: ["vacancies", offset, appliedFilters],
    placeholderData: (previous) => previous,
    enabled: !hasFilterErrors && !hasAppliedFilterErrors,
    queryFn: async () => {
      const params = buildVacancyParams(appliedFilters, true, offset);
      const query = params.toString();
      const response = await api<VacancyPage | Vacancy[]>(query ? `/vacancies?${query}` : "/vacancies");
      return Array.isArray(response)
        ? { items: response, total: response.length, has_more: false }
        : response ?? { items: [], total: 0, has_more: false };
    },
  });
  useEffect(() => {
    if (q.data?.items && !q.isPlaceholderData && allVacanciesKey === appliedFilterKey) {
      setAllVacancies((previous) => offset === 0
        ? q.data!.items
        : [...previous, ...q.data!.items.filter((item) => !previous.some((existing) => existing.id === item.id))]);
    }
  }, [q.data, q.isPlaceholderData, offset, allVacanciesKey, appliedFilterKey]);
  const displayedVacancies = !hasFilterErrors && allVacanciesKey === appliedFilterKey ? allVacancies : [];
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
        <label>Статус<select aria-label="Статус" value={filters.status_group} onChange={(event) => setFilter("status_group", event.target.value)}><option value="">Все</option>{VACANCY_STATUS_OPTIONS_WITH_CANCELLED.map((status) => <option value={status.value} key={status.value}>{status.label}</option>)}</select></label>
        <fieldset className="vacancy-filter-range vacancy-filter-date"><legend>Дата</legend><label>От<input aria-label="Дата от" type="date" value={filters.status_date_from} {...filterInputProps("status_date_from")} onChange={(event) => setFilter("status_date_from", event.target.value)} />{filterError("status_date_from")}</label><label>До<input aria-label="Дата до" type="date" value={filters.status_date_to} {...filterInputProps("status_date_to")} onChange={(event) => setFilter("status_date_to", event.target.value)} />{filterError("status_date_to")}</label></fieldset>
        <fieldset className="vacancy-filter-range"><legend>Общий балл</legend><label>От<input aria-label="Общий балл от" type="number" min="0" max="100" value={filters.total_score_min} {...filterInputProps("total_score_min")} onChange={(event) => setFilter("total_score_min", event.target.value)} />{filterError("total_score_min")}</label><label>До<input aria-label="Общий балл до" type="number" min="0" max="100" value={filters.total_score_max} {...filterInputProps("total_score_max")} onChange={(event) => setFilter("total_score_max", event.target.value)} />{filterError("total_score_max")}</label></fieldset>
        </div>
        <details className="vacancy-filter-criteria" open={criteriaOpen} onToggle={(event) => setCriteriaOpen(event.currentTarget.open)}>
          <summary aria-expanded={criteriaOpen} aria-controls="vacancy-criteria-fields"><span><strong>Баллы по критериям</strong><small>Уточните минимальные и максимальные значения для каждого критерия.</small></span><ChevronIcon className="vacancy-filter-chevron" /></summary>
          <div className="vacancy-filter-criteria-grid" id="vacancy-criteria-fields">
        {VACANCY_FILTER_CRITERIA.map((criterion) => {
          const minimumKey = `${criterion.key}_min` as keyof VacancyFilters;
          const maximumKey = `${criterion.key}_max` as keyof VacancyFilters;
          return <fieldset className="vacancy-filter-range" key={criterion.key}><legend>{criterion.title}</legend><label>От<input aria-label={`${criterion.title} от`} type="number" min="0" max="100" value={filters[minimumKey]} {...filterInputProps(minimumKey as keyof VacancyFilterRangeFields)} onChange={(event) => setFilter(minimumKey, event.target.value)} />{filterError(minimumKey as keyof VacancyFilterRangeFields)}</label><label>До<input aria-label={`${criterion.title} до`} type="number" min="0" max="100" value={filters[maximumKey]} {...filterInputProps(maximumKey as keyof VacancyFilterRangeFields)} onChange={(event) => setFilter(maximumKey, event.target.value)} />{filterError(maximumKey as keyof VacancyFilterRangeFields)}</label></fieldset>;
        })}
          </div>
        </details>
        <div className="vacancy-filter-secondary">
        <label>Сайт<select aria-label="Сайт" value={filters.site} onChange={(event) => setFilter("site", event.target.value)}><option value="">Все</option><option value="HH.ru">HH.ru</option><option value="HireHi">HireHi</option><option value="Zarplata.ru">Zarplata.ru</option><option value="__legacy__">Без сайта</option></select></label>
        <label>Сортировка<select aria-label="Сортировка" value={filters.sort} onChange={(event) => setFilter("sort", event.target.value)}><option value="date">Дата</option><option value="state">Статус</option><option value="total_score">Общий балл</option>{VACANCY_FILTER_CRITERIA.map((criterion) => <option value={criterion.key} key={criterion.key}>{criterion.title}</option>)}<option value="site">Сайт</option><option value="title">Название вакансии</option><option value="id">Номер вакансии</option></select></label>
        <label>Направление<select aria-label="Направление" value={filters.sort_dir} onChange={(event) => setFilter("sort_dir", event.target.value)}><option value="desc">По убыванию</option><option value="asc">По возрастанию</option></select></label>
        </div>
        <div className="vacancy-export-actions" aria-label="Экспорт вакансий">{(["csv", "xlsx", "xml"] as const).map((format) => hasFilterErrors
          ? <button key={format} type="button" className="button-link secondary" disabled aria-disabled="true">{format.toUpperCase()}</button>
          : <a key={format} className="button-link secondary" download href={vacancyExportUrl(filters, format)}>{format.toUpperCase()}</a>)}</div>
      </div>
      {hasFilterErrors ? <Notice tone="danger" role="alert">Исправьте отмеченные фильтры: список вакансий и экспорт появятся после исправления.</Notice> : q.isFetching && q.data ? <Notice role="status">{"\u041e\u0431\u043d\u043e\u0432\u043b\u044f\u0435\u043c \u0441\u043f\u0438\u0441\u043e\u043a \u0432\u0430\u043a\u0430\u043d\u0441\u0438\u0439\u2026"}</Notice> : null}
      {!hasFilterErrors && q.error && q.data && <Notice tone="danger" role="alert">{"\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043e\u0431\u043d\u043e\u0432\u0438\u0442\u044c \u0441\u043f\u0438\u0441\u043e\u043a; \u043f\u043e\u043a\u0430\u0437\u044b\u0432\u0430\u0435\u043c \u043f\u043e\u0441\u043b\u0435\u0434\u043d\u0438\u0435 \u0434\u0430\u043d\u043d\u044b\u0435."}</Notice>}
      {!hasFilterErrors && (!q.data && q.isLoading ? <Notice role="status">{"\u0417\u0430\u0433\u0440\u0443\u0437\u043a\u0430 \u0432\u0430\u043a\u0430\u043d\u0441\u0438\u0439"}</Notice> : q.error && !q.data ? <Notice tone="danger" role="alert">{"\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u0437\u0430\u0433\u0440\u0443\u0437\u0438\u0442\u044c \u0432\u0430\u043a\u0430\u043d\u0441\u0438\u0438."}</Notice> : displayedVacancies.length ? (
        <div className="vacancy-list">
          {displayedVacancies.map((v) => <VacancyCard key={v.id} v={v} />)}
          {q.data?.has_more && <button type="button" className="secondary" onClick={() => setOffset((value) => value + 30)} disabled={q.isFetching}>{"\u041f\u043e\u043a\u0430\u0437\u0430\u0442\u044c \u0435\u0449\u0451"}</button>}
        </div>
      ) : (
        <Empty>
          {"\u0417\u0430\u043f\u0443\u0441\u0442\u0438\u0442\u0435 \u0441\u0435\u0441\u0441\u0438\u044e \u043d\u0430 \u0432\u044b\u0431\u0440\u0430\u043d\u043d\u043e\u0439 \u043f\u043b\u043e\u0449\u0430\u0434\u043a\u0435, \u0447\u0442\u043e\u0431\u044b \u0443\u0432\u0438\u0434\u0435\u0442\u044c \u043d\u0430\u0439\u0434\u0435\u043d\u043d\u044b\u0435 \u0432\u0430\u043a\u0430\u043d\u0441\u0438\u0438."}
        </Empty>
      ))}
    </section>
  );
}

function ModelPage() {
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["model-status"],
    queryFn: () => api<ModelStatus>("/model/status"),
    retry: false,
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
  const catalogHealth = catalogHealthPresentation(q.data, q.isLoading, q.isError);
  const generationHealth = generationHealthPresentation(q.data, q.isLoading, q.isError);
  const generation = q.data?.generation_health;
  const lastGeneration = generation?.healthy === false ? generation.last_failure : generation?.last_success;
  return (
    <section className="page">
      <Title
        eyebrow="ПОДКЛЮЧЕНИЕ МОДЕЛИ"
        note="Подключите OpenAI, совместимый облачный сервис или локальный сервер."
      >
        OpenAI API
      </Title>
      <article className="panel model model-health-panel">
        <div className="model-health-grid">
          <section className="model-health-item" aria-labelledby="catalog-health-heading">
            <div className={`orb ${catalogHealth.complete ? "online" : ""}`} aria-hidden="true"></div>
            <div>
              <span className="model-health-kicker">Доступность каталога</span>
              <Status value={catalogHealth.code} label={catalogHealth.label} />
              <h2 id="catalog-health-heading">{q.data?.model || "Модель не указана"}</h2>
              <p>{catalogHealth.detail}</p>
            </div>
          </section>
          <section className="model-health-item" aria-labelledby="generation-health-heading">
            <div className={`orb ${generationHealth.complete ? "online" : ""}`} aria-hidden="true"></div>
            <div>
              <span className="model-health-kicker">Работоспособность генерации</span>
              <Status value={generationHealth.code} label={generationHealth.label} />
              <h2 id="generation-health-heading">Реальные запросы</h2>
              <p>{generationHealth.detail}</p>
              {generation && <p className="model-health-counts">Успешно: {generation.success_count} · Ошибок: {generation.failure_count} · В работе: {generation.running} · В очереди: {generation.queued}</p>}
              {lastGeneration && <small>Последнее наблюдение: <time dateTime={lastGeneration.at}>{formatServerUtc(lastGeneration.at)}</time>{lastGeneration.diagnostic_id && <> · диагностика {lastGeneration.diagnostic_id}</>}</small>}
            </div>
          </section>
        </div>
        <div className="model-health-actions">
          <code>Ключ хранится в защищённом хранилище DPAPI</code>
          <button type="button" className="secondary" onClick={() => { void qc.invalidateQueries({ queryKey: ["model-status"] }); void qc.invalidateQueries({ queryKey: ["model-settings"] }); }}>
          Проверить снова
          </button>
        </div>
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
  useEffect(() => { purgeLegacyResumeSourceStorage(); }, []);
  return (
    <Shell>
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/profile" element={<ResumeSourcesPage />} />
        <Route path="/session" element={<SessionPage />} />
        <Route path="/vacancies" element={<VacanciesPage />} />
        <Route path="/model" element={<ModelPage />} />
      </Routes>
    </Shell>
  );
}
