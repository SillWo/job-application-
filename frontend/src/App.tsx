import { ReactNode, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { NavLink, Route, Routes, useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Toaster, toast } from "sonner";
import { Select } from "@base-ui/react/select";
import { Popover } from "@base-ui/react/popover";
import { AlertDialog } from "@base-ui/react/alert-dialog";
import { Dialog } from "@base-ui/react/dialog";
import { api } from "./api";
import { formatUtcTimestampLocal, localDateTimeBounds } from "./vacancyDates";
import { validateVacancyFilters, type VacancyFilterRangeFields } from "./vacancyFilterValidation";
import { useSessionDraft } from "./useSessionDraft";
import type {
  JobSession,
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
  resumeQuestions,
  isResumeQuestionValid,
  previewSections,
  resumeContactLabel,
  statusLabel,
  publicResumeSourceUrl,
  safeResumeUrlLabel,
  resumeCompletionStatus,
  safeCompletionErrorMessage,
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
  const analysisStatus = detail.analysis_status ?? base.analysis_status;
  const historyContext = detail.history_context ?? base.history_context;
  // A history mirror has no current evaluation. Do not allow a stale score in
  // either endpoint to become its current score during detail merging.
  if (analysisStatus === "not_evaluated_history") {
    return { ...base, ...detail, analysis_status: analysisStatus, history_context: historyContext, evaluation: null };
  }
  const evaluation = detail.evaluation;
  const directScore = typeof detail.score === "number" ? detail.score : undefined;
  const directDecision = typeof detail.decision === "string" ? detail.decision : undefined;
  const directConfidence = typeof detail.confidence === "number" ? detail.confidence : undefined;
  const directCategory = typeof detail.category === "string" ? detail.category : undefined;
  const directReason = typeof detail.reason === "string" ? detail.reason : undefined;
  const directBreakdown = detail.score_breakdown ?? undefined;
  if (!evaluation && directScore === undefined && directDecision === undefined && directConfidence === undefined && directCategory === undefined && directReason === undefined && directBreakdown === undefined) return { ...base, ...detail, analysis_status: analysisStatus, history_context: historyContext };
  const fallback = base.evaluation;
  return {
    ...base,
    ...detail,
    analysis_status: analysisStatus,
    history_context: historyContext,
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
  CREATED: { label: "Создана", tone: "warning" }, PREPARING: { label: "Подготовка", tone: "warning" }, RUNNING: { label: "В работе", tone: "warning" }, STOPPING: { label: "Остановка", tone: "warning" }, PROCESSING: { label: "В процессе", tone: "warning" }, LOADING: { label: "Проверяем", tone: "warning" }, PENDING: { label: "Ожидание генерации", tone: "warning" },
  PAUSED: { label: "Приостановлена", tone: "info" }, STOPPED: { label: "Остановлена", tone: "info" },
  COMPLETED: { label: "Завершена", tone: "success" }, SUCCESS: { label: "Успех", tone: "success" }, CONNECTED: { label: "Соединение есть", tone: "success" }, AVAILABLE: { label: "Модель доступна", tone: "success" }, HEALTHY: { label: "Генерация работает", tone: "success" },
  PARTIAL: { label: "Резюме отправлено, письмо не завершено", tone: "warning" },
  FAILED: { label: "Ошибка", tone: "danger" }, REJECTED: { label: "Отклонена", tone: "danger" }, ERROR: { label: "Ошибка", tone: "danger" }, DISCONNECTED: { label: "Нет соединения", tone: "danger" }, UNAVAILABLE: { label: "Модель недоступна", tone: "danger" }, UNHEALTHY: { label: "Ошибка генерации", tone: "danger" }, STATUS_ERROR: { label: "Статус недоступен", tone: "danger" },
  UNKNOWN: { label: "Ещё не проверено", tone: "neutral" },
  CANCELLED: { label: "Отменена", tone: "neutral" },
};
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
function statusTone(value?: string) { return value ? STATUS_META[value]?.tone ?? "neutral" : "neutral"; }
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
  { value: "SUCCESS", label: "Успех" }, { value: "PARTIAL", label: "Резюме отправлено, письмо не завершено" }, { value: "PROCESSING", label: "В процессе" }, { value: "REJECTED", label: "Отклонена" }, { value: "ERROR", label: "Ошибка" },
] as const;
const VACANCY_STATUS_OPTIONS_WITH_CANCELLED = [...VACANCY_STATUS_OPTIONS, { value: "CANCELLED", label: "\u041e\u0442\u043c\u0435\u043d\u0435\u043d\u0430" }] as const;
function vacancyStatusGroup(vacancy: Vacancy): VacancyStatusGroup {
  const dataErrorCode = typeof vacancy.data?.error_code === "string" ? vacancy.data.error_code : undefined;
  const legacyStatusGroup = (vacancy as { status_group?: unknown }).status_group;
  if (vacancy.error_code === "SUBMISSION_UNCONFIRMED" || dataErrorCode === "SUBMISSION_UNCONFIRMED" || vacancy.state === "SUBMISSION_UNCONFIRMED" || vacancy.state === "UNCONFIRMED" || legacyStatusGroup === "UNCONFIRMED") return "ERROR";
  if (vacancy.state === "CANCELLED" || vacancy.status_group === "CANCELLED") return "CANCELLED";
  if (vacancy.state === "PARTIAL" || vacancy.status_group === "PARTIAL") return "PARTIAL";
  if (vacancy.status_group === "SUCCESS" || vacancy.status_group === "PROCESSING" || vacancy.status_group === "REJECTED" || vacancy.status_group === "ERROR") {
    return vacancy.status_group;
  }
  if (["SUBMITTED", "ALREADY_APPLIED", "REPORTED"].includes(vacancy.state)) return "SUCCESS";
  if (vacancy.state === "REJECTED_BY_MODEL") return "REJECTED";
  if (vacancy.state === "ERROR") return "ERROR";
  return "PROCESSING";
}
function isUnconfirmedSubmission(vacancy: Vacancy) {
  const dataErrorCode = typeof vacancy.data?.error_code === "string" ? vacancy.data.error_code : undefined;
  const legacyStatusGroup = (vacancy as { status_group?: unknown }).status_group;
  return vacancy.error_code === "SUBMISSION_UNCONFIRMED" || dataErrorCode === "SUBMISSION_UNCONFIRMED" || vacancy.state === "SUBMISSION_UNCONFIRMED" || vacancy.state === "UNCONFIRMED" || legacyStatusGroup === "UNCONFIRMED";
}
function isHistoricalNoEvaluation(vacancy: Vacancy) {
  return vacancy.analysis_status === "not_evaluated_history";
}
function historyNoEvaluationMessage(vacancy: Vacancy) {
  const context = vacancy.history_context;
  let message: string;
  switch (context?.outcome) {
    case "unconfirmed":
      message = "Предыдущую отправку не удалось подтвердить. Повторно отклик автоматически не отправлялся; сверьте результат на площадке, чтобы согласовать историю. Это не ошибка оценки модели.";
      break;
    case "partial":
      message = "Новая оценка не выполнялась: предыдущая обработка завершилась частично, поэтому повторный отклик не запускался. Это не ошибка оценки модели.";
      break;
    case "already_applied":
      message = "Новая оценка не выполнялась: на эту вакансию уже откликались, поэтому повторный отклик не отправлялся. Это не ошибка оценки модели.";
      break;
    default:
      message = "Новая оценка не выполнялась: предыдущий отклик уже учтён, поэтому повторная отправка не запускалась. Это не ошибка оценки модели.";
  }
  const references = [
    context?.source_vacancy_id != null ? `предыдущая вакансия #${context.source_vacancy_id}` : "",
    context?.source_session_id != null ? `сессия #${context.source_session_id}` : "",
  ].filter(Boolean);
  return references.length ? `${message} Источник: ${references.join(", ")}.` : message;
}
function vacancyOutcome(vacancy: Vacancy) {
  if (vacancyStatusGroup(vacancy) === "CANCELLED") return "Обработка вакансии отменена до завершения.";
  if (vacancy.state === "SUBMITTED") return "Отклик действительно отправлен после положительной оценки вакансии.";
  if (vacancy.state === "ALREADY_APPLIED") return "Новый отклик не отправлялся: вы уже откликались на эту вакансию.";
  if (isUnconfirmedSubmission(vacancy)) return "Результат прошлой отправки не подтверждён. Новый отклик автоматически не отправлялся; проверьте вакансию на площадке и согласуйте результат с историей.";
  if (vacancy.state === "REPORTED") return "Вакансия добавлена в отчёт, внешний отклик не отправлялся.";
  if (vacancyStatusGroup(vacancy) === "PARTIAL") return "Резюме отправлено, письмо не завершено.";
  if (vacancyStatusGroup(vacancy) === "REJECTED") return "Модель отклонила вакансию из-за недостаточной релевантности, поэтому отклик не отправлен.";
  if (vacancyStatusGroup(vacancy) === "ERROR") return "Результат обработки вакансии не подтверждён.";
  return "Обработка вакансии ещё идёт.";
}
function vacancyStatusLabel(vacancy: Vacancy): string | undefined {
  const site = (vacancy.site || vacancy.source || "").toLowerCase();
  if (vacancy.state === "ALREADY_APPLIED") return "Уже откликались";
  if (isUnconfirmedSubmission(vacancy)) return "Отправка не подтверждена";
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
  return <p className={`notice notice-${tone}`} data-tone={tone} role={role ?? (tone === "danger" ? "alert" : "status")}>{children}</p>;
}
function Status({ value, label }: { value: string; label?: string }) {
  const meta = STATUS_META[value];
  return (
    <span className={`status status-${meta?.tone ?? "neutral"} s-${value.toLowerCase()}`} data-status={value} data-tone={meta?.tone ?? "neutral"}>
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

type ResumeImportErrors = Record<string, string>;
const RESUME_IMPORT_ERRORS_KEY = ["resume-source-import-errors"] as const;

function updateResumeImportError(queryClient: ReturnType<typeof useQueryClient>, adapterId: string, message?: string) {
  const current = queryClient.getQueryData<ResumeImportErrors>(RESUME_IMPORT_ERRORS_KEY) ?? {};
  const next = { ...current };
  if (message) next[adapterId] = safeCompletionErrorMessage(message) ?? "Не удалось извлечь данные из резюме.";
  else delete next[adapterId];
  queryClient.setQueryData(RESUME_IMPORT_ERRORS_KEY, next);
}

function useResumeImportErrors() {
  return useQuery<ResumeImportErrors>({
    queryKey: RESUME_IMPORT_ERRORS_KEY,
    queryFn: async () => ({}),
    initialData: {},
    staleTime: Infinity,
  }).data;
}

function resumeCompletionError(record: ResumeSourceRecord | undefined, importError: string | undefined): string | undefined {
  return safeCompletionErrorMessage(record?.completionErrorMessage)
    ?? (record?.resumeDataStatus === "missing" || record?.resumeDataStatus === "corrupt"
      ? safeCompletionErrorMessage(record.resumeDataErrorMessage)
      : undefined)
    ?? (record?.completionStatus === "error" ? "Не удалось обработать сохранённое резюме." : undefined)
    ?? (record ? undefined : safeCompletionErrorMessage(importError));
}

function resumeLaunchMessage(adapterId: string, status: ReturnType<typeof resumeCompletionStatus>): string | undefined {
  const label = resumeSite(adapterId).label;
  if (status === "empty") return `Для сайта ${label} не загружено резюме, проверьте раздел "Профиль"`;
  if (status === "error") return `Для сайта ${label} не удалось извлечь необходимые данные из резюме, проверьте раздел "Профиль"`;
  return undefined;
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
  const importErrors = useResumeImportErrors();
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
  // Every supported source needs a ready local copy before a session can use it.
  const hasResume = Object.entries(resumeSources).some(([adapterId, source]) => source.confirmed && ["complete", "partial"].includes(resumeCompletionStatus(source, importErrors[adapterId])));
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
            <span className={catalogReadiness.complete ? "is-done" : ""} data-tone={statusTone(catalogReadiness.code)}><OverviewIcon name="check" />{catalogReadiness.label}</span>
            <span className={generationReadiness.complete ? "is-done" : ""} data-tone={statusTone(generationReadiness.code)}><OverviewIcon name="check" />{generationReadiness.label}</span>
            <span className={hasProfile ? "is-done" : ""} data-tone={hasProfile ? "success" : "neutral"}><OverviewIcon name="check" />Источники {hasProfile ? "добавлены" : "не настроены"}</span>
            <span className={hasResume ? "is-done" : ""} data-tone={hasResume ? "success" : "neutral"}><OverviewIcon name="check" />Резюме {hasResume ? "подтверждено" : "не подтверждено"}</span>
            <span data-tone={statusTone(active?.status)}><i key={`status-pulse-strip-${active?.status ?? "none"}`} className={sessionReady ? "overview-status-pulse" : ""} />Сессия {activeStatus.toLowerCase()}</span>
          </div>
        </div>
        <div className="overview-preview" aria-label="Статус рабочего процесса">
          <div className="preview-top" data-tone={statusTone(active?.status)}><span key={`status-pulse-preview-${active?.status ?? "none"}`} className={`preview-dot${sessionReady ? " overview-status-pulse" : ""}`} />Рабочий процесс <span className="preview-live">{activeStatus}</span></div>
          <div className="preview-job"><span className="preview-logo">J</span><div><strong>Подходящие вакансии</strong><small>Оценка по резюме и условиям сессии</small></div><b>{sessionReady ? "Оценивает" : active ? activeStatus : hasResume ? "Готово к запуску" : "Нужно настроить"}</b></div>
          <div className="preview-lines">
            <div className="preview-line"><i className={hasProfile ? "is-done" : ""} data-tone={hasProfile ? "success" : "neutral"}>{hasProfile ? "✓" : "1"}</i><span>Источники резюме</span><small>{hasProfile ? "Добавлены" : "Нужно настроить"}</small></div>
            <div className="preview-line"><i className={hasResume ? "is-done" : ""} data-tone={hasResume ? "success" : "neutral"}>{hasResume ? "✓" : "2"}</i><span>Подтверждённое резюме</span><small>{hasResume ? "Готово к оценке" : "Добавьте ссылку"}</small></div>
            <div className="preview-line"><i className={sessionReady ? "is-done" : ""} data-tone={statusTone(active?.status)}>{sessionReady ? "✓" : "3"}</i><span>Наблюдение за сессией</span><small>{active ? activeStatus : "Настройте критерии и лимиты"}</small></div>
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

type ResumeSaveCandidate = { preview: ResumePreview; previewToken: string; grammaticalGender?: "male" | "female" };

function ResumeSourceIcon({ name }: { name: "edit" | "cancel" | "delete" }) {
  return <svg className="resume-source-action-icon" viewBox="0 0 18 18" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">
    {name === "edit" && <><path d="m3 12.8-.6 2.8 2.8-.6L14 6.2 11.8 4 3 12.8Z" /><path d="m10.8 5 2.2 2.2" /></>}
    {name === "cancel" && <path d="m4 4 10 10M14 4 4 14" />}
    {name === "delete" && <><path d="M3 5h12M7 5V3h4v2m2 0-.7 10H5.7L5 5m2.5 2v5m3-5v5" /></>}
  </svg>;
}

function ResumePreviewCard({ adapterId, record, queryClient, sourcesBlocked }: {
  adapterId: string;
  record: ResumeSourceRecord | undefined;
  queryClient: ReturnType<typeof useQueryClient>;
  sourcesBlocked: boolean;
}) {
  const site = resumeSite(adapterId);
  const importErrors = useResumeImportErrors();
  const importError = importErrors[adapterId];
  const completion = resumeCompletionStatus(record, importError);
  const completionError = completion === "error" ? resumeCompletionError(record, importError) ?? "Не удалось извлечь необходимые данные из резюме." : undefined;
  const completionPresentation = {
    empty: { label: "Не заполнено", tone: "neutral" },
    complete: { label: "Заполнено", tone: "success" },
    partial: { label: "Частично", tone: "warning" },
    error: { label: "Ошибка", tone: "danger" },
  }[completion];
  const savedSourceUrl = publicResumeSourceUrl(record?.sourceUrl, adapterId)
    ?? publicResumeSourceUrl(record?.preview.source_url, adapterId)
    ?? "";
  const [url, setUrl] = useState(savedSourceUrl);
  const [editing, setEditing] = useState(false);
  const [busyAction, setBusyAction] = useState<"preview" | "save" | "refresh" | "delete" | null>(null);
  const busyRef = useRef(false);
  const urlInputRef = useRef<HTMLInputElement>(null);
  const deleteTriggerRef = useRef<HTMLButtonElement>(null);
  const deleteCancelRef = useRef<HTMLButtonElement>(null);
  const [error, setError] = useState("");
  const [completionHelpOpen, setCompletionHelpOpen] = useState(false);
  const [notice, setNotice] = useState<{ tone: "warning"; text: string } | null>(null);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [genderOpen, setGenderOpen] = useState(false);
  const [genderChoice, setGenderChoice] = useState<"male" | "female" | "">("");
  const [pendingSave, setPendingSave] = useState<ResumeSaveCandidate | null>(null);
  useEffect(() => {
    setUrl(savedSourceUrl);
    setEditing(false);
    setPendingSave(null);
    setGenderOpen(false);
    setGenderChoice("");
    setDeleteOpen(false);
    setError("");
    setNotice(null);
  }, [adapterId, savedSourceUrl]);

  const setBusy = (action: "preview" | "save" | "refresh" | "delete" | null) => {
    busyRef.current = action !== null;
    setBusyAction(action);
  };
  const updateCachedRecord = (updated: ResumeSourceRecord) => {
    const current = queryClient.getQueryData<Record<string, ResumeSourceRecord>>(["resume-sources"]) ?? {};
    queryClient.setQueryData(["resume-sources"], { ...current, [adapterId]: updated });
    updateResumeImportError(queryClient, adapterId);
  };
  const performConfirm = async (candidate: ResumeSaveCandidate, gender?: "male" | "female") => {
    if (busyRef.current) return;
    setBusy("save");
    setError("");
    setNotice(null);
    try {
      const result = await api<unknown>("/resume-sources/confirm", {
        method: "POST",
        body: JSON.stringify({ adapter_id: adapterId, preview_token: candidate.previewToken, consent: true, ...(gender ? { grammatical_gender: gender } : {}) }),
      });
      const updated = sourceRecordFromResponse(result);
      if (!updated) throw new Error("Сервис не вернул сохранённое резюме.");
      if (updated.adapterId !== adapterId) throw new Error("Сохранённое резюме вернулось для другой площадки.");
      updateCachedRecord(updated);
      updateResumeImportError(queryClient, adapterId);
      setEditing(false);
      setPendingSave(null);
      setGenderOpen(false);
      setGenderChoice("");
      setUrl(publicResumeSourceUrl(updated.sourceUrl, adapterId) ?? publicResumeSourceUrl(updated.preview.source_url, adapterId) ?? "");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Не удалось сохранить резюме.");
    } finally {
      setBusy(null);
    }
  };
  const save = async () => {
    if (busyRef.current || sourcesBlocked) return;
    setError("");
    setNotice(null);
    if (record && !editing) {
      setBusy("refresh");
      try {
        const result = await api<unknown>(`/resume-sources/${adapterId}/refresh`, { method: "POST" });
        const updated = sourceRecordFromResponse(result);
        if (!updated) throw new Error("Сервис не вернул обновлённое резюме.");
        if (updated.adapterId !== adapterId) throw new Error("Обновлённое резюме вернулось для другой площадки.");
        updateCachedRecord(updated);
        if (updated.status === "unavailable") {
          setNotice({ tone: "warning", text: updated.resumeDataStatus === "ready" ? "Не удалось обновить резюме. Сохранённая копия данных остаётся доступной." : "Сайт не подтвердил актуальность резюме." });
        }
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : "Не удалось обновить резюме.");
      } finally {
        setBusy(null);
      }
      return;
    }

    const safeUrl = publicResumeSourceUrl(url, adapterId);
    if (!safeUrl) {
      setError(`Укажите прямую ссылку на резюме ${site.label} по HTTPS без параметров.`);
      return;
    }
    setBusy("preview");
    let extractionFailed = true;
    try {
      const result = await api<ResumePreviewResponse>("/resume-sources/preview", {
        method: "POST",
        body: JSON.stringify({ adapter_id: adapterId, resume_url: safeUrl }),
      });
      const preview = previewFromResponse(result);
      if (typeof result.preview_token !== "string" || result.preview_token.trim().length < 20) throw new Error("Сервис не выдал действительный токен проверки.");
      if (preview.source_site && preview.source_site !== adapterId) throw new Error("Сайт в ответе не совпал с выбранной площадкой.");
      const unsupportedQuestion = resumeQuestions(preview).find((question) => question.required !== false && question.id !== "grammatical_gender");
      if (unsupportedQuestion) throw new Error(`Нельзя сохранить резюме: требуется неподдерживаемый ответ «${unsupportedQuestion.question}».`);
      const genderQuestion = resumeQuestions(preview).find((question) => question.id === "grammatical_gender" && question.required !== false);
      const candidate: ResumeSaveCandidate = {
        preview,
        previewToken: result.preview_token,
        ...(genderQuestion && record?.grammaticalGender ? { grammaticalGender: record.grammaticalGender } : {}),
      };
      if (genderQuestion && !record?.grammaticalGender) {
        setPendingSave(candidate);
        setGenderChoice("");
        setGenderOpen(true);
        setBusy(null);
        return;
      }
      setBusy(null);
      extractionFailed = false;
      await performConfirm(candidate, candidate.grammaticalGender);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Не удалось проверить ссылку на резюме.");
      if (extractionFailed && (!record || record.resumeDataStatus !== "ready")) {
        updateResumeImportError(queryClient, adapterId, reason instanceof Error ? reason.message : "Не удалось извлечь необходимые данные из резюме.");
      }
      setBusy(null);
    }
  };
  const cancelEditing = () => {
    setUrl(savedSourceUrl);
    setEditing(false);
    setError("");
    setNotice(null);
  };
  const startEditing = () => {
    if (busyRef.current || sourcesBlocked) return;
    setEditing(true);
    setError("");
    setNotice(null);
    requestAnimationFrame(() => urlInputRef.current?.focus());
  };
  const confirmGender = () => {
    if (!pendingSave || (genderChoice !== "male" && genderChoice !== "female") || !isResumeQuestionValid({ id: "grammatical_gender", question: "Род для сопроводительных писем", required: true, options: ["male", "female"] }, genderChoice)) return;
    const candidate = pendingSave;
    setGenderOpen(false);
    setPendingSave(null);
    void performConfirm(candidate, genderChoice);
  };
  const cancelGender = () => {
    setGenderOpen(false);
    setPendingSave(null);
    setGenderChoice("");
    setError("");
  };
  const remove = async () => {
    if (busyRef.current) return;
    if (!record) {
      updateResumeImportError(queryClient, adapterId);
      setUrl("");
      setError("");
      setNotice(null);
      return;
    }
    setBusy("delete");
    setError("");
    try {
      await api<unknown>(`/resume-sources/${adapterId}`, { method: "DELETE" });
      const current = queryClient.getQueryData<Record<string, ResumeSourceRecord>>(["resume-sources"]) ?? {};
      const next = { ...current };
      delete next[adapterId];
      queryClient.setQueryData(["resume-sources"], next);
      updateResumeImportError(queryClient, adapterId);
      setDeleteOpen(false);
      setEditing(false);
      setUrl("");
      setPendingSave(null);
      setGenderOpen(false);
      setGenderChoice("");
      setError("");
      setNotice(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Не удалось удалить резюме.");
    } finally {
      setBusy(null);
    }
  };

  const sections = record ? previewSections(record.preview) : [];
  const contacts = [
    ...(record?.preview.contacts?.found ?? []).map((label) => ({ label: resumeContactLabel(label), status: "present" })),
    ...(record?.preview.contacts?.hidden ?? []).map((label) => ({ label: resumeContactLabel(label), status: "hidden" })),
  ];
  const receivedItems = [...sections];
  for (const contact of contacts) {
    if (!receivedItems.some((item) => item.label === contact.label)) receivedItems.push(contact);
  }
  const statusIcon = (label: string, status: string) => {
    const present = status === "present";
    const accessibleStatus = statusLabel(status);
    return <span className="resume-section-status" data-tone={present ? "success" : "danger"} role="img" aria-label={`${label}: ${accessibleStatus}`} title={accessibleStatus}>
      <svg viewBox="0 0 18 18" width="18" height="18" fill="none" aria-hidden="true" focusable="false" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        {present ? <path d="m4 9 3.2 3.2L14 5.8" /> : <path d="m5 5 8 8M13 5l-8 8" />}
      </svg>
    </span>;
  };
  const controlsDisabled = sourcesBlocked || busyAction !== null || deleteOpen || genderOpen;
  const primaryLabel = busyAction === "preview" ? "Проверяем…"
    : busyAction === "save" ? "Сохраняем…"
    : busyAction === "refresh" ? "Обновляем…"
      : record ? editing ? "Сохранить изменения" : "Обновить данные" : "Сохранить ссылку";
  const deleteAddress = savedSourceUrl || (record?.maskedUrl ? safeResumeUrlLabel(record.maskedUrl) : "Ссылка сохранена");
  return <article className="panel resume-source-card" aria-labelledby={`resume-source-${adapterId}`}>
    <div className="resume-completion-row">
      <span className="resume-completion-status status" data-tone={completionPresentation.tone} role="status" aria-label={completionPresentation.label}>{completionPresentation.label}</span>
      {completionError && <span className="resume-completion-help" onMouseEnter={() => setCompletionHelpOpen(true)} onMouseLeave={() => setCompletionHelpOpen(false)}>
        <button type="button" className="resume-completion-help-trigger" aria-label={`Причина ошибки для ${site.label}`} aria-describedby={`resume-completion-error-${adapterId}`} onFocus={() => setCompletionHelpOpen(true)} onBlur={() => setCompletionHelpOpen(false)}>
          <svg viewBox="0 0 20 20" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true" focusable="false"><circle cx="10" cy="10" r="7.5" /><path d="M10 9v4m0-6h.01" strokeLinecap="round" /></svg>
        </button>
        {completionHelpOpen && <span className="resume-completion-tooltip" id={`resume-completion-error-${adapterId}`} role="tooltip">{completionError}</span>}
      </span>}
    </div>
    <h2 id={`resume-source-${adapterId}`}>{site.label}</h2>
    <label className="profile-full-field">Ссылка на резюме на {site.label}
      <input ref={urlInputRef} className="resume-source-url-input" type="url" aria-label={`Ссылка на резюме на ${site.label}`} value={url} onChange={(event) => { setUrl(event.target.value); setError(""); setNotice(null); if (!record && !event.target.value.trim()) updateResumeImportError(queryClient, adapterId); }} placeholder={`https://${site.id === "hh" ? "hh.ru" : site.id === "hirehi" ? "hirehi.ru" : "zarplata.ru"}/resume/...`} autoComplete="off" readOnly={Boolean(record && !editing)} disabled={controlsDisabled} />
    </label>
    <div className="resume-source-actions">
      <button type="button" className="primary resume-source-primary-action" onClick={() => void save()} disabled={controlsDisabled || ((!record || editing) && !url.trim())}>{primaryLabel}</button>
      <div className="resume-source-secondary-actions">
        <button type="button" className="secondary resume-source-icon-action" aria-label={editing ? `Отменить редактирование ссылки на ${site.label}` : `Редактировать ссылку на ${site.label}`} title={editing ? "Отменить редактирование" : "Редактировать ссылку"} onClick={editing ? cancelEditing : startEditing} disabled={controlsDisabled || !record}>
          <ResumeSourceIcon name={editing ? "cancel" : "edit"} />
        </button>
        <button ref={deleteTriggerRef} type="button" className="secondary resume-source-icon-action" aria-label={record ? `Удалить резюме для ${site.label}` : `Сбросить ошибку импорта для ${site.label}`} title={record ? "Удалить резюме" : "Сбросить ошибку импорта"} onClick={() => { setError(""); if (record) setDeleteOpen(true); else void remove(); }} disabled={controlsDisabled || (!record && !importError)}>
          <ResumeSourceIcon name="delete" />
        </button>
      </div>
    </div>
    {error && !deleteOpen && !genderOpen && <Notice tone="danger" role="alert">{error}</Notice>}
    {notice && <Notice tone={notice.tone}>{notice.text}</Notice>}
    {record && <details className="resume-coverage">
      <summary className="resume-coverage-summary"><span>Полученные данные</span><ChevronIcon className="resume-coverage-chevron" /></summary>
      <ul className="resume-source-list">
        {receivedItems.length > 0
          ? receivedItems.map((item) => <li key={`${item.label}-${item.status}`}><span>{item.label}</span>{statusIcon(item.label, item.status)}</li>)
          : <li className="resume-source-empty">Платформа не передала разделы или контакты.</li>}
      </ul>
    </details>}

    <AlertDialog.Root open={deleteOpen} onOpenChange={(open) => { if (!busyRef.current) setDeleteOpen(open); }}>
      <AlertDialog.Portal>
        <AlertDialog.Backdrop className="resume-source-dialog-backdrop" />
        <AlertDialog.Viewport className="resume-source-dialog-viewport">
          <AlertDialog.Popup className="resume-source-dialog" initialFocus={deleteCancelRef} finalFocus={() => record ? deleteTriggerRef.current : urlInputRef.current}>
            <AlertDialog.Title className="resume-source-dialog-title">Удалить резюме?</AlertDialog.Title>
            <AlertDialog.Description className="resume-source-dialog-description">Вы точно хотите удалить резюме {deleteAddress} для сайта {site.label}?</AlertDialog.Description>
            {error && deleteOpen ? <Notice tone="danger" role="alert">{error}</Notice> : null}
            <div className="resume-source-dialog-actions">
              <AlertDialog.Close render={<button ref={deleteCancelRef} type="button" className="secondary" disabled={busyAction === "delete"} />}>Нет</AlertDialog.Close>
              <button type="button" className="danger" onClick={() => void remove()} disabled={busyAction === "delete"}>{busyAction === "delete" ? "Удаляем…" : "Да"}</button>
            </div>
          </AlertDialog.Popup>
        </AlertDialog.Viewport>
      </AlertDialog.Portal>
    </AlertDialog.Root>

    <Dialog.Root open={genderOpen} onOpenChange={(open) => { if (!busyRef.current) { if (open) setGenderOpen(true); else cancelGender(); } }}>
      <Dialog.Portal>
        <Dialog.Backdrop className="resume-source-dialog-backdrop" />
        <Dialog.Viewport className="resume-source-dialog-viewport">
          <Dialog.Popup className="resume-source-dialog">
            <Dialog.Title className="resume-source-dialog-title">Род для сопроводительных писем</Dialog.Title>
            <Dialog.Description className="resume-source-dialog-description">Какой род использовать в сопроводительных письмах?</Dialog.Description>
            <label className="profile-full-field">Род
              <select aria-label="Род" value={genderChoice} onChange={(event) => setGenderChoice(event.target.value === "male" || event.target.value === "female" ? event.target.value : "")}>
                <option value="">Выберите вариант</option>
                <option value="male">Мужской</option>
                <option value="female">Женский</option>
              </select>
            </label>
            {error && <Notice tone="danger" role="alert">{error}</Notice>}
            <div className="resume-source-dialog-actions">
              <Dialog.Close render={<button type="button" className="secondary" onClick={cancelGender} />}>Отмена</Dialog.Close>
              <button type="button" className="primary" onClick={confirmGender} disabled={!genderChoice || busyAction !== null}>Сохранить</button>
            </div>
          </Dialog.Popup>
        </Dialog.Viewport>
      </Dialog.Portal>
    </Dialog.Root>
  </article>;
}

function ResumeSourcesPage() {
  const queryClient = useQueryClient();
  const sourcesQuery = useQuery({ queryKey: ["resume-sources"], queryFn: () => fetchResumeSources(), staleTime: 30_000, refetchInterval: false, refetchOnWindowFocus: false });
  const sources = sourcesQuery.data ?? {};
  const sourcesLoadError = sourcesQuery.isError && !sourcesQuery.data;
  const sourcesBlocked = sourcesQuery.isPending || sourcesLoadError;
  return <section className="page profile-sources-page">
    <Title eyebrow="ИСТОЧНИКИ РЕЗЮМЕ" note="Ссылка проверяется и сохраняется после нажатия кнопки.">Профиль — ссылки на резюме</Title>
    {sourcesLoadError && <><Notice tone="danger" role="alert">Не удалось загрузить сохранённые резюме.</Notice><button type="button" className="secondary" onClick={() => void sourcesQuery.refetch()} disabled={sourcesQuery.isFetching}>{sourcesQuery.isFetching ? "Загружаем…" : "Повторить"}</button></>}
    <div className="resume-source-grid">{RESUME_SITES.map((site) => <ResumePreviewCard key={site.id} adapterId={site.id} record={sources[site.id]} queryClient={queryClient} sourcesBlocked={sourcesBlocked} />)}</div>
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

const SEEN_FAILURES_STORAGE_KEY = "job-orchestrator.seen-session-failures";

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
  const importErrors = useResumeImportErrors();
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
  const savedDataRequired = adapter === "hh" || adapter === "hirehi" || adapter === "zarplata";
  const currentResumeStatus = resumeCompletionStatus(resumeSource, importErrors[adapter]);
  const resumeDataReady = savedDataRequired && (currentResumeStatus === "complete" || currentResumeStatus === "partial");
  const profileReady = Boolean(resumeSource && resumeDataReady && !missingGenderPreference);
  const resumeDataBlocked = Boolean(savedDataRequired && currentResumeStatus === "error");
  const sourceFetchBlocked = resumeSourcesQuery.isPending || (resumeSourcesQuery.isError && !resumeSourcesQuery.data);
  const desiredJobDescriptionRef = useRef<HTMLTextAreaElement>(null);
  const idempotencyKeyRef = useRef<string | null>(null);
  const createInFlightRef = useRef(false);
  const [pendingActions, setPendingActions] = useState<Record<string, boolean>>({});
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
      // Launches use the durable local copy captured when the source was
      // confirmed or explicitly refreshed.
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
    if (sourceFetchBlocked) return;
    const blockedMessage = resumeLaunchMessage(adapter, currentResumeStatus);
    if (blockedMessage) {
      toast.error(blockedMessage);
      return;
    }
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
    if (name === "start") {
      if (sourceFetchBlocked) return;
      const session = (sessions.data ?? []).find((item) => item.id === sessionId);
      const sessionAdapter = session?.adapter_id ?? adapter;
      const sessionResumeStatus = resumeCompletionStatus(resumeSources[sessionAdapter], importErrors[sessionAdapter]);
      const blockedMessage = resumeLaunchMessage(sessionAdapter, sessionResumeStatus);
      if (blockedMessage) {
        toast.error(blockedMessage);
        return;
      }
    }
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
      setMessageTone(name === "stop" ? "warning" : "success");
      setMessage(actionMessage);
      if (name === "stop") toast.warning(actionMessage); else toast.success(actionMessage);
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
            disabled={sourceFetchBlocked || (resumeDataReady && missingGenderPreference) || create.isPending || !limitsAreValid || !coverLetterIsValid || !coverLetterMaxWordsAreValid || blockedByAdapter}
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
          {!profileReady && !resumeDataBlocked && !missingGenderPreference && (
            <Notice tone="info">Добавьте ссылку на резюме сайта {resumeSite(adapter).label} во вкладке <NavLink to="/profile">Профиль</NavLink>.</Notice>
          )}
          {resumeDataBlocked && <Notice tone="info">Обновите данные резюме во вкладке <NavLink to="/profile">Профиль</NavLink>.</Notice>}
          {missingGenderPreference && <Notice tone="info">Укажите род для сопроводительных писем в <NavLink to="/profile">профиле</NavLink>.</Notice>}
        </article>
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
        <summary className="session-history-head"><ChevronIcon className="session-history-chevron" /><strong id="session-history-heading">Завершённые сессии ({history.data.total})</strong><span className="session-history-total">{history.data.total}</span></summary>
        {history.error ? <p className="vacancy-error-message" role="alert">Не удалось загрузить историю сессий.</p> : historyItems.length === 0 ? <p className="empty-score">История пока пуста.</p> : <>
          {historyItems.map((session) => <SessionCard key={session.id} session={session} formatSessionLimit={formatSessionLimit} action={action} pendingAction={Object.keys(pendingActions).find((key) => key.startsWith(`${session.id}:`))?.split(":")[1]} />)}
          <div className="session-history-pagination"><button type="button" className="secondary" onClick={() => setHistoryOffset((value) => Math.max(0, value - 10))} disabled={historyOffset === 0 || history.isFetching}>Назад</button><span>Показаны {historyOffset + 1}–{Math.min(historyOffset + historyItems.length, history.data?.total ?? historyOffset + historyItems.length)}</span><button type="button" className="secondary" onClick={() => setHistoryOffset((value) => value + 10)} disabled={!history.data?.has_more || history.isFetching}>Дальше</button></div>
        </>}
      </details> : null}
    </section>
  );
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
                Лимит сессии: {session.adapter_id === "hirehi" ? "выбрано" : "принятые резюме, включая частичные"} — {formatSessionLimit(session.application_limit)}.
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
            {terminal && <HireHiReport session={session} />}
          </article>
          <div className={`stats compact ${session.adapter_id === "hirehi" ? "session-stats-four" : "session-stats-five"}`}>
            {[
              ["Просмотрено", "viewed"],
              ["Отфильтровано", "filtered"],
              [session.adapter_id === "hirehi" ? "В отчёте" : "Отправлено", session.adapter_id === "hirehi" ? "reported" : "submitted"],
              ...(session.adapter_id === "hirehi" ? [] : [["Резюме отправлено, письмо не завершено", "partial"]]),
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
  const listHistoricalNoEvaluation = isHistoricalNoEvaluation(normalizedVacancy);
  const hasFullEvaluation = Boolean(normalizedVacancy.evaluation && (Array.isArray(normalizedVacancy.evaluation.score_breakdown) || normalizedVacancy.evaluation.reason));
  const legacySubmissionUnconfirmed = vacancyStatusGroup(normalizedVacancy) === "ERROR" && vacancyErrorCode(normalizedVacancy) === "SUBMISSION_UNCONFIRMED";
  const detail = useQuery({
    queryKey: ["vacancy-detail", rawVacancy.id],
    queryFn: () => api<Vacancy>(`/vacancies/${rawVacancy.id}`),
    // The list payload already contains the contractual legacy error. Keep
    // it visible while expanding instead of showing a lazy-detail spinner.
    enabled: open && !hasFullEvaluation && !legacySubmissionUnconfirmed && !listHistoricalNoEvaluation,
    staleTime: 60_000,
    retry: false,
  });
  const fetchedDetail = detail.data && !Array.isArray(detail.data) ? detail.data : undefined;
  const displayed = fetchedDetail ? detailedVacancy(normalizedVacancy, fetchedDetail) : normalizedVacancy;
  const historicalNoEvaluation = isHistoricalNoEvaluation(displayed);
  const evaluation = displayed.evaluation;
  const breakdown = evaluationRows(evaluation);
  const v = { ...displayed, evaluation: evaluation as NonNullable<Vacancy["evaluation"]> };
  const statusGroup = vacancyStatusGroup(v);
  const errorMessage = vacancyErrorMessage(v);
  return <details className="panel vacancy-score vacancy-disclosure" open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary aria-expanded={open} aria-controls={`vacancy-details-${v.id}`}>
      <span className="vacancy-main"><b>{v.title}</b><small>#{v.id} · {v.company || "Компания не указана"}{v.site ? ` · ${v.site}` : ""}</small></span>
      <span className="score-total"><strong>{historicalNoEvaluation ? "—" : v.evaluation?.score ?? "—"}</strong><small>{historicalNoEvaluation ? "Не оценивалась" : "/ 100"}</small></span>
      <span><Status value={statusGroup} label={vacancyStatusLabel(v)} />{v.status_changed_at && <small className="vacancy-status-date">{formatStatusDate(v.status_changed_at)}</small>}</span>
      <span className="vacancy-disclosure-control"><span className="sr-only">{open ? "Скрыть подробности вакансии" : "Показать подробности вакансии"}</span><ChevronIcon className="vacancy-chevron" /></span>
    </summary>
    {historicalNoEvaluation ? <div className="score-details" id={`vacancy-details-${v.id}`}>
      <div className="resume-score-heading"><span className="eyebrow">КРАТКОЕ РЕЗЮМЕ</span><small>Оценка не выполнялась</small></div>
      <p className="vacancy-history-explanation">{historyNoEvaluationMessage(v)}</p>
      {isUnconfirmedSubmission(v) && <p className="vacancy-error-code">Код ошибки: {vacancyErrorCode(v)}</p>}
      <a href={v.url} target="_blank" rel="noreferrer">Открыть вакансию на площадке</a>
    </div> : detail.isLoading && !legacySubmissionUnconfirmed && statusGroup !== "ERROR" ? <p className="empty-score" role="status">Загрузка подробностей…</p> : detail.error && statusGroup !== "ERROR" ? <p className="vacancy-error-message" role="alert">Не удалось загрузить подробности вакансии.</p> : (hasFullEvaluation || detail.data) && evaluation ? <div className="score-details" id={`vacancy-details-${v.id}`}>
      <div className="resume-score-heading"><span className="eyebrow">КРАТКОЕ РЕЗЮМЕ</span></div>
      <p>{humanModelSummary(evaluation.reason || "", statusGroup, breakdown)}</p>
      {statusGroup === "ERROR" ? <><p className="vacancy-error-message">{errorMessage}</p><p className="vacancy-error-code">Код ошибки: {vacancyErrorCode(v)}</p></> : <p>{vacancyOutcome(v)}</p>}
      <div className="resume-score-heading"><span className="eyebrow">ПО КРИТЕРИЯМ</span></div>
      {presentationBreakdown(v.evaluation.score_breakdown ?? []).map((row) => <div className="score-row" key={row.key}><div><b>{row.title}</b><span>{row.max_points > 0 ? `${row.points} / ${row.max_points}` : "не применяется"}</span></div>{row.max_points > 0 && <div className="scorebar" role="progressbar" aria-label={`Релевантность: ${row.title}`} aria-valuenow={row.points} aria-valuemin={0} aria-valuemax={row.max_points}><i style={{ width: `${Math.min(100, Math.max(0, (row.points / row.max_points) * 100))}%` }} /></div>}<small>{humanCriterionExplanation(row.explanation)}</small></div>)}
      <a href={v.url} target="_blank" rel="noreferrer">Открыть вакансию на площадке</a>
    </div> : statusGroup === "PARTIAL"
      ? <div className="score-details" id={`vacancy-details-${v.id}`}><p>{vacancyOutcome(v)}</p><a href={v.url} target="_blank" rel="noreferrer">Открыть вакансию на площадке</a></div>
      : statusGroup === "ERROR"
      ? <div className="score-details" id={`vacancy-details-${v.id}`}><div className="resume-score-heading"><span className="eyebrow">КРАТКОЕ РЕЗЮМЕ</span><small>Что произошло с вакансией</small></div><p className="vacancy-error-message">{errorMessage}</p><p className="vacancy-error-code">Код ошибки: {vacancyErrorCode(v)}</p><a href={v.url} target="_blank" rel="noreferrer">Открыть вакансию на площадке</a></div>
      : statusGroup === "CANCELLED"
      ? <div className="score-details" id={`vacancy-details-${v.id}`}><p>{vacancyOutcome(v)}</p><a href={v.url} target="_blank" rel="noreferrer">Открыть вакансию на площадке</a></div>
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
      {hasFilterErrors ? <Notice tone="danger" role="alert">Исправьте отмеченные фильтры: список вакансий и экспорт появятся после исправления.</Notice> : q.isFetching && q.data ? <Notice tone="warning" role="status">{"\u041e\u0431\u043d\u043e\u0432\u043b\u044f\u0435\u043c \u0441\u043f\u0438\u0441\u043e\u043a \u0432\u0430\u043a\u0430\u043d\u0441\u0438\u0439\u2026"}</Notice> : null}
      {!hasFilterErrors && q.error && q.data && <Notice tone="danger" role="alert">{"\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043e\u0431\u043d\u043e\u0432\u0438\u0442\u044c \u0441\u043f\u0438\u0441\u043e\u043a; \u043f\u043e\u043a\u0430\u0437\u044b\u0432\u0430\u0435\u043c \u043f\u043e\u0441\u043b\u0435\u0434\u043d\u0438\u0435 \u0434\u0430\u043d\u043d\u044b\u0435."}</Notice>}
      {!hasFilterErrors && (!q.data && q.isLoading ? <Notice tone="warning" role="status">{"\u0417\u0430\u0433\u0440\u0443\u0437\u043a\u0430 \u0432\u0430\u043a\u0430\u043d\u0441\u0438\u0439"}</Notice> : q.error && !q.data ? <Notice tone="danger" role="alert">{"\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u0437\u0430\u0433\u0440\u0443\u0437\u0438\u0442\u044c \u0432\u0430\u043a\u0430\u043d\u0441\u0438\u0438."}</Notice> : displayedVacancies.length ? (
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
            <div className={`orb ${catalogHealth.complete ? "online" : ""}`} data-tone={statusTone(catalogHealth.code)} aria-hidden="true"></div>
            <div>
              <span className="model-health-kicker">Доступность каталога</span>
              <Status value={catalogHealth.code} label={catalogHealth.label} />
              <h2 id="catalog-health-heading">{q.data?.model || "Модель не указана"}</h2>
              <p>{catalogHealth.detail}</p>
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
