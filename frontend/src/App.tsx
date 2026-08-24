import { ReactNode, useEffect, useId, useRef, useState } from "react";
import { NavLink, Route, Routes, useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./api";
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
  { key: "title", title: "Название должности", maxPoints: 2, weight: 5 },
  { key: "tasks", title: "Задачи", maxPoints: 3, weight: 30 },
  { key: "industry", title: "Сфера", maxPoints: 4, weight: 25 },
  { key: "required_years", title: "Годы опыта", maxPoints: 2, weight: 20 },
  { key: "languages", title: "Языки", maxPoints: 2, weight: 10 },
  { key: "skills", title: "Навыки", maxPoints: 3, weight: 10 },
];

const INFLUENCE_CRITERIA = [
  { key: "tasks", title: "Задачи", hint: "ИИ оценивает сходство обязанностей и задач вакансии с опытом в резюме." },
  { key: "industry", title: "Сфера", hint: "ИИ оценивает соответствие отрасли и домена вакансии опыту кандидата." },
  { key: "skills", title: "Навыки", hint: "ИИ оценивает соответствие требуемых навыков навыкам из резюме." },
] as const;
const INFLUENCE_LEVELS = ["low", "medium", "high"] as const;
type InfluenceLevel = (typeof INFLUENCE_LEVELS)[number];

function presentationBreakdown(rows: ScoreComponent[]): ScoreComponent[] {
  return RELEVANCE_CRITERIA.map((criterion) => {
    const row = rows.find((candidate) => candidate.key === criterion.key);
    const hasRawScore = typeof row?.raw_points === "number" && typeof row?.raw_max_points === "number";
    const points = hasRawScore
      ? Math.min(criterion.maxPoints, Math.max(0, row!.raw_points!))
      : Math.round(Math.min(1, Math.max(0, (row?.points ?? 0) / (row?.max_points || 1))) * criterion.maxPoints);
    const rawPoints = hasRawScore
      ? Math.min(criterion.maxPoints, Math.max(0, row!.raw_points!))
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

const nav = [
  ["/", "Обзор"],
  ["/profile", "Профиль"],
  ["/sites", "Сайты"],
  ["/session", "Сессия"],
  ["/vacancies", "Вакансии"],
  ["/model", "Модель"],
];

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
          {nav.map(([to, label]) => (
            <NavLink key={to} to={to} end={to === "/"}>
              {label}
            </NavLink>
          ))}
        </nav>
        <div className="topbar-actions"><Notifications /><a href="/docs" target="_blank">API</a></div></div></header>
      <main>{children}
      </main>
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
function Empty({ children }: { children: ReactNode }) {
  return (
    <div className="empty">
      <b>Пока пусто</b>
      <p>{children}</p>
    </div>
  );
}
function Status({ value }: { value: string }) {
  return (
    <span className={`status s-${value.toLowerCase()}`}>
      {value.replaceAll("_", " ")}
    </span>
  );
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
  const active = sessions.data?.[0];
  return (
    <section className="page">
      <Title
        eyebrow="ЦЕНТР УПРАВЛЕНИЯ"
        note="Один спокойный маршрут от резюме до проверенного решения."
      >
        Ваш поиск работы — под контролем
      </Title>
      <div className="stats">
        <article>
          <small>Профили</small>
          <strong>{profiles.data?.length ?? "—"}</strong>
          <span>личные профили</span>
        </article>
        <article>
          <small>Последняя сессия</small>
          <strong>{active?.status ?? "Нет"}</strong>
          <span>{active?.adapter_id ?? "создайте первую"}</span>
        </article>
      </div>
      <div className="grid2">
        <article className="panel">
          <div className="panelhead">
            <div>
              <span className="eyebrow">БЫСТРЫЙ СТАРТ</span>
              <h2>Готовность к сессии</h2>
            </div>
          </div>
          {[
            ["1", "Заполните личный профиль", Boolean(profiles.data?.length)],
            ["2", "Выберите резюме для оценки", Boolean(dashboardResumes.data?.some((resume) => resume.selected_for_matching))],
            ["3", "Запустите сессию на HH.ru", !!active],
          ].map(([n, text, done]) => (
            <div className="step" key={String(n)}>
              <b className={done ? "done" : ""}>{done ? "✓" : n}</b>
              <span>{String(text)}</span>
            </div>
          ))}
        </article>
        <article className="panel dark">
          <span className="eyebrow">ПРИНЦИП РАБОТЫ</span>
          <h2>ИИ ищет подходящие вакансии</h2>
          <p>
            Ваш запрос на естественном языке учитывается при оценке каждой вакансии.
            На HireHi подходящие вакансии собираются в PDF-отчёт без отправки откликов.
            На HH.ru отклики возможны после проверки пользователем.
          </p>
          <div className="flow">
            <span>Запрос</span>
            <i>→</i>
            <span>Оценить</span>
            <i>→</i>
            <span>Проверить и получить отчёт</span>
          </div>
        </article>
      </div>
    </section>
  );
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
          {value.contacts.messengers.map((messenger, index) => <div className="profile-remove-row" key={index}><input className="profile-field" aria-label={`Мессенджер ${index + 1}`} value={messenger} onChange={(e) => change("contacts", { ...value.contacts, messengers: value.contacts.messengers.map((item, itemIndex) => itemIndex === index ? e.target.value : item) })} placeholder="Telegram, WhatsApp или другой мессенджер" /><button type="button" className="icon-button" aria-label={`Удалить мессенджер ${index + 1}`} onClick={() => change("contacts", { ...value.contacts, messengers: value.contacts.messengers.filter((_, itemIndex) => itemIndex !== index) })}>×</button></div>)}
        </div>
      </div>
      <div className="subsection form-rail">
        <div className="section-heading"><div><h3>Образование</h3><small>Выберите тип для каждого учебного заведения.</small></div><button type="button" className="secondary" onClick={() => change("education", [...value.education, newEducation()])}>+ Образование</button></div>
        <div className="repeat-list">
          {value.education.map((item, index) => <div className="nested-card education-card" key={index}>
            <div className="profile-remove-row profile-education-header"><select className="profile-field" aria-label={`Тип образования ${index + 1}`} value={item.type} onChange={(e) => changeEducation(index, { type: e.target.value as EducationType })}>{educationOptions.map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select><button type="button" className="icon-button" aria-label={`Удалить образование ${index + 1}`} onClick={() => change("education", value.education.filter((_, itemIndex) => itemIndex !== index))}>×</button></div>
            <div className={item.type === "school" ? "profile-full-field" : "profile-pair-row"}>
              <label className="profile-field">{item.type === "higher" ? "Университет" : "Учебное заведение"}<input value={item.institution} onChange={(e) => changeEducation(index, { institution: e.target.value })} /></label>
              {item.type !== "school" && <label className="profile-field">Факультет<input value={item.faculty ?? ""} onChange={(e) => changeEducation(index, { faculty: e.target.value })} /></label>}
            </div>
            {item.type === "higher" && <label className="profile-full-field">Степень<select value={item.degree ?? ""} onChange={(e) => changeEducation(index, { degree: (e.target.value || null) as Degree | null })}><option value="">Выберите степень</option><option value="bachelor">Бакалавр</option><option value="master">Магистр</option><option value="specialist">Специалист</option><option value="postgraduate">Аспирант</option></select></label>}
            {item.type !== "school" && <label className="profile-full-field">Специальность<input value={item.specialty ?? ""} onChange={(e) => changeEducation(index, { specialty: e.target.value })} /></label>}
            <div className="profile-pair-row"><label className="profile-field">Дата начала обучения<input type="month" value={item.start_date ?? ""} onChange={(e) => changeEducation(index, { start_date: e.target.value || null })} /></label><label className="profile-field">Дата окончания обучения<input type="month" value={item.end_date ?? ""} onChange={(e) => changeEducation(index, { end_date: e.target.value || null })} /></label></div>
          </div>)}
        </div>
      </div>
      <div className="subsection form-rail">
        <div className="section-heading"><div><h3>Языки</h3><small>Язык и уровень владения.</small></div><button type="button" className="secondary" onClick={() => change("languages", [...value.languages, { language: "", proficiency: "" }])}>+ Язык</button></div>
        <div className="repeat-list">{value.languages.map((language, index) => <div className="profile-pair-remove-row" key={index}><input className="profile-field" aria-label={`Язык ${index + 1}`} value={language.language} onChange={(e) => changeLanguage(index, { language: e.target.value })} placeholder="Например, английский" /><input className="profile-field" aria-label={`Уровень языка ${index + 1}`} value={language.proficiency} onChange={(e) => changeLanguage(index, { proficiency: e.target.value })} placeholder="Например, B2" /><button type="button" className="icon-button" aria-label={`Удалить язык ${index + 1}`} onClick={() => change("languages", value.languages.filter((_, itemIndex) => itemIndex !== index))}>×</button></div>)}</div>
      </div>
      <label className="checkline"><input type="checkbox" checked={value.driver_license ?? false} onChange={(e) => change("driver_license", e.target.checked)} /> Есть водительские права</label>
      <div className="actions"><button type="button" onClick={onSave} disabled={saving}>{saving ? "Сохраняю…" : "Сохранить личный профиль"}</button></div>
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
  return <div className="multi-select field-wide">
    <button type="button" className="multi-select-trigger" aria-label={label} aria-expanded={open} aria-controls={listId} onClick={() => setOpen((current) => !current)}>
      <span className="multi-select-trigger-copy"><span className="multi-select-label">{label}</span><span className="multi-select-value">{selectedLabels.join(", ") || "Выберите варианты"}</span></span>
      <span className={`multi-select-chevron${open ? " is-open" : ""}`} aria-hidden="true">⌄</span>
    </button>
    {hint && <small className="multi-select-hint">{hint}</small>}
    {open && <div className="multi-select-popover" id={listId}>
      <div className="multi-select-options">
        {options.map(([value, optionLabel]) => <label className={`multi-select-option${values.includes(value) ? " is-selected" : ""}`} key={value}>
          <input type="checkbox" checked={values.includes(value)} onChange={() => onToggle(value)} />
          <span>{optionLabel}</span>
        </label>)}
      </div>
      <button type="button" className="multi-select-confirm" onClick={() => setOpen(false)}>Выбрать</button>
    </div>}
  </div>;
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
  return <div className="notifications" onBlur={(event) => { if (!event.currentTarget.contains(event.relatedTarget as Node)) setOpen(false); }}>
    <button type="button" className="notification-trigger" aria-label="Уведомления" aria-expanded={open} onClick={() => setOpen((value) => !value)}>
      <svg className="notification-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">
        <path d="M18 9a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9" />
        <path d="M10 21h4" />
      </svg>{unread > 0 && <i role="status" className="notification-badge" aria-label={`${unread} непрочитанных`} />}
    </button>
    {open && <div className="notification-popover" role="dialog" aria-label="Уведомления">
      <div className="notification-head"><strong>Уведомления</strong><button type="button" onClick={() => void readAll()} disabled={!unread}>Прочитать все</button></div>
      {query.isLoading ? <p className="notification-empty">Загрузка…</p> : notifications.length === 0 ? <p className="notification-empty">Новых уведомлений нет</p> : <div className="notification-list">{notifications.map((item) => <button type="button" className={`notification-item${item.read_at ? "" : " unread"}`} key={item.id} onClick={() => void markRead(item)}><strong>{item.title}</strong><span>{item.message}</span><time dateTime={item.created_at}>{new Date(item.created_at).toLocaleString("ru-RU")}</time></button>)}</div>}
    </div>}
  </div>;
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
    <div className="panelhead"><div><span className="eyebrow">БЛОК 2</span><h2>{draft.id ? "Редактирование резюме" : "Новое резюме"}</h2></div><button type="button" className="icon-button" onClick={onCancel} aria-label="Закрыть редактор резюме">×</button></div>
    <div className="form-content">
    <label className="field-medium">Название резюме<input value={draft.name} onChange={(e) => update({ name: e.target.value })} placeholder="Например, Product Manager" /></label>
    <div className="row form-row"><label className="field-medium">Предполагаемая должность<input value={draft.desired_title ?? ""} onChange={(e) => update({ desired_title: e.target.value })} /></label><label className="field-compact">Желаемый доход<input value={draft.desired_salary ?? ""} onChange={(e) => update({ desired_salary: e.target.value })} placeholder="Например, 180 000 ₽" /></label></div>
    <MultiSelect label="Желаемый тип занятости" options={employmentOptions} values={draft.employment_types} onToggle={(type) => toggleEmployment(type as EmploymentType)} />
    <MultiSelect label="Формат работы" hint="Можно выбрать несколько вариантов." options={formatOptions} values={draft.work_formats} onToggle={(format) => update({ work_formats: draft.work_formats.includes(format) ? draft.work_formats.filter((item) => item !== format) : [...draft.work_formats, format] })} />
    <label className="field-compact">Командировки<select value={draft.business_trips === null || draft.business_trips === undefined ? "" : draft.business_trips ? "can" : "cannot"} onChange={(e) => update({ business_trips: e.target.value === "" ? null : e.target.value === "can" })}><option value="">Выберите вариант</option><option value="can">Могу</option><option value="cannot">Не могу</option></select></label>
    <div className="subsection form-rail"><div className="section-heading"><div><h3>Опыт работы</h3><small>Добавьте должности и обязанности.</small></div><button type="button" className="secondary" onClick={() => update({ experiences: [...draft.experiences, newExperience()] })}>+ Опыт</button></div><div className="repeat-list">{draft.experiences.map((experience, index) => <div className="nested-card experience-card" key={index}><div className="repeat-row"><strong>Опыт #{index + 1}</strong><button type="button" className="icon-button" aria-label={`Удалить опыт ${index + 1}`} onClick={() => update({ experiences: draft.experiences.filter((_, itemIndex) => itemIndex !== index) })}>×</button></div><div className="row form-row"><label className="field-medium">Компания<input value={experience.company} onChange={(e) => updateExperience(index, { company: e.target.value })} /></label><label className="field-medium">Должность<input value={experience.position} onChange={(e) => updateExperience(index, { position: e.target.value })} /></label></div><div className="row form-row"><label className="field-compact">Начало работы<input type="month" value={experience.start_date ?? ""} onChange={(e) => updateExperience(index, { start_date: e.target.value || null })} /></label><label className="field-compact">Конец работы<input type="month" value={experience.end_date ?? ""} onChange={(e) => updateExperience(index, { end_date: e.target.value || null })} placeholder="Оставьте пустым, если работаете сейчас" /></label></div><label className="field-prose">Описание обязанностей<textarea value={experience.duties} onChange={(e) => updateExperience(index, { duties: e.target.value })} /></label></div>)}</div></div>
    <label className="field-wide">Навыки<small>Введите навыки через запятую — каждый станет отдельным тегом.</small><input value={skillsText} onChange={(e) => setSkillsText(e.target.value)} placeholder="CustDev, Scrum, аналитика" /></label>
    <div className="tag-list field-wide" aria-label="Навыки">{skillsText.split(",").map((skill) => skill.trim()).filter(Boolean).map((skill) => <span className="tag" key={skill}>{skill}</span>)}</div>
    <label className="field-prose">О себе<textarea className="about-text" value={draft.about} onChange={(e) => { const words = e.target.value.trim() ? e.target.value.trim().split(/\s+/u) : []; if (words.length <= 500) update({ about: e.target.value }); }} /><small className={wordCount > 500 ? "word-limit" : ""}>{wordCount} / 500 слов</small></label>
    <div className="actions form-rail"><button type="button" onClick={() => onSave({ ...draft, skills: skillsText.split(",").map((skill) => skill.trim()).filter(Boolean) })} disabled={saving}>{saving ? "Сохраняю…" : "Сохранить резюме"}</button><button type="button" className="secondary" onClick={onCancel}>Отмена</button></div>
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
  const savePersonal = useMutation({ mutationFn: () => current ? api<Profile>(`/profiles/${current.id}`, { method: "PATCH", body: JSON.stringify(personalPayload(personal)) }) : api<Profile>("/profiles", { method: "POST", body: JSON.stringify(personalPayload(personal)) }), onSuccess: async () => { setMessage("Личный профиль сохранён."); await qc.invalidateQueries({ queryKey: ["profiles"] }); }, onError: (e) => setMessage(e.message) });
  const saveResume = useMutation({ mutationFn: (resume: Resume) => current ? resume.id ? api<Resume>(`/profiles/${current.id}/resumes/${resume.id}`, { method: "PATCH", body: JSON.stringify(resumePayload(resume)) }) : api<Resume>(`/profiles/${current.id}/resumes`, { method: "POST", body: JSON.stringify(resumePayload(resume)) }) : Promise.reject(new Error("Сначала создайте профиль.")), onSuccess: async () => { setEditingResume(null); setMessage("Резюме сохранено."); await qc.invalidateQueries({ queryKey: ["resumes", current?.id] }); }, onError: (e) => setMessage(e.message) });
  const removeResume = useMutation({ mutationFn: (id: number) => current ? api(`/profiles/${current.id}/resumes/${id}`, { method: "DELETE" }) : Promise.reject(new Error("Профиль не найден.")), onSuccess: async () => { setMessage("Резюме удалено."); await qc.invalidateQueries({ queryKey: ["resumes", current?.id] }); }, onError: (e) => setMessage(e.message) });
  const upload = useMutation({ mutationFn: async (file: File) => { if (!current) throw new Error("Сначала сохраните личный профиль."); const body = new FormData(); body.append("file", file); return api<{ profile: Profile; resume: Resume }>(`/profiles/${current.id}/resumes/import`, { method: "POST", body }); }, onMutate: (file) => setMessage(`Файл «${file.name}» принят. Заполняем профиль и отдельное резюме…`), onSuccess: async (result) => { qc.setQueryData<Profile[]>(["profiles"], (items = []) => [result.profile, ...items.filter((item) => item.id !== result.profile.id)]); qc.setQueryData<Resume[]>(["resumes", result.profile.id], (items = []) => [result.resume, ...items.filter((item) => item.id !== result.resume.id)]); setMessage(`Импорт «${result.resume.name || result.resume.original_filename || "резюме"}» завершён. Проверьте и отредактируйте поля.`); await qc.invalidateQueries({ queryKey: ["profiles"] }); }, onError: (e) => setMessage(e.message) });
  const toggleSelected = async (resume: Resume) => { if (!current) return; try { await api<Resume>(`/profiles/${current.id}/resumes/${resume.id}`, { method: "PATCH", body: JSON.stringify(resumePayload({ ...resume, selected_for_matching: !resume.selected_for_matching })) }); await qc.invalidateQueries({ queryKey: ["resumes", current.id] }); } catch (error) { setMessage(error instanceof Error ? error.message : "Не удалось изменить выбор резюме"); } };
  return <section className="page">
    <Title eyebrow="ПРОФИЛЬ КАНДИДАТА" note="Заполните личные данные один раз, а затем создавайте отдельные резюме под разные направления поиска. Только выбранные резюме попадут в оценку вакансий.">Профиль и резюме под вашим контролем</Title>
    {!current && <article className="panel empty-profile"><h2>Создайте личный профиль</h2><p>Начните с личной информации. После сохранения можно импортировать PDF, DOCX или TXT и отредактировать результат.</p><button type="button" onClick={() => savePersonal.mutate()} disabled={savePersonal.isPending}>{savePersonal.isPending ? "Создаём…" : "Создать профиль"}</button></article>}
    {current && <>
      <label className={`upload ${upload.isPending ? "busy" : ""}`}><input type="file" accept=".pdf,.docx,.txt" aria-label="Импортировать резюме" disabled={upload.isPending} onChange={(event) => { const file = event.currentTarget.files?.[0]; event.currentTarget.value = ""; if (file) upload.mutate(file); }} /><span>{upload.isPending ? `Обрабатываем «${upload.variables?.name}»…` : "Импортировать PDF, DOCX или TXT"}</span><small>Парсер заполнит личный профиль и создаст отдельное резюме. После импорта проверьте поля.</small></label>
      {message && <p className={`notice ${upload.isError || savePersonal.isError ? "error" : ""}`} role="status">{message}</p>}
      <PersonalEditor value={personal} onChange={setPersonal} onSave={() => savePersonal.mutate()} saving={savePersonal.isPending} />
      <section className="resume-section"><div className="section-heading resume-section-heading"><div><span className="eyebrow">БЛОК 2</span><h2>Мои резюме</h2><p>Отметьте одно или несколько резюме, которые передавать ИИ при оценке релевантности вакансий.</p></div><button type="button" onClick={() => setEditingResume(newResume(current.id))}>+ Создать резюме</button></div>
        {editingResume && <ResumeEditor value={editingResume} onSave={(value) => saveResume.mutate(value)} onCancel={() => setEditingResume(null)} saving={saveResume.isPending} />}
        {resumes.data?.length ? <div className="resume-grid">{resumes.data.map((resume) => <article className={`panel resume-card ${resume.selected_for_matching ? "selected" : ""}`} key={resume.id}><div className="resume-card-top"><div><span className="eyebrow">РЕЗЮМЕ</span><h3>{resume.name || resume.desired_title || "Без названия"}</h3></div><label className="selection-control"><input type="checkbox" checked={resume.selected_for_matching} onChange={() => void toggleSelected(resume)} /> Передавать модели</label></div><div className="resume-meta"><span>{resume.desired_title || "Должность не указана"}</span><span>{resume.skills?.length ?? 0} навыков</span><span>{resume.experiences?.length ?? 0} мест опыта</span></div><p>{resume.about || "Добавьте короткое описание о себе как о работнике."}</p><div className="tag-list">{resume.skills?.slice(0, 8).map((skill) => <span className="tag" key={skill}>{skill}</span>)}</div><div className="actions"><button type="button" className="secondary" onClick={() => setEditingResume(resume)}>Редактировать</button><button type="button" className="danger" onClick={() => { if (window.confirm("Удалить это резюме?")) removeResume.mutate(resume.id); }}>Удалить</button></div></article>)}</div> : <div className="empty"><b>Резюме пока нет</b><p>Создайте резюме вручную или импортируйте файл сверху.</p></div>}
      </section>
    </>}
  </section>;
}

function SitesPage() {
  const { data } = useQuery({
    queryKey: ["adapters"],
    queryFn: () => api<Adapter[]>("/adapters"),
  });
  return (
    <section className="page">
      <Title
        eyebrow="ПОДКЛЮЧЁННЫЕ САЙТЫ"
        note="Каждая площадка работает в отдельном постоянном профиле Chromium."
      >
        Адаптеры без скрытых API
      </Title>
      <div className="cards">
        {data?.map((a) => (
          <article className="panel" key={a.site_id}>
            <span className="adapterlogo">hh</span>
            <h2>{a.display_name}</h2>
            <p>
              Поиск, анализ и отправка откликов после ручного входа в аккаунт.
            </p>
            <div className="panelhead">
              <Status value="Доступен" />
              <small>{a.allowed_domains.join(", ")}</small>
            </div>
          </article>
        ))}
      </div>
    </section>
  );
}

function SessionPage() {
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
  const [adapter, setAdapter] = useState("hh");
  const blockedByAdapter = (sessions.data ?? []).some((session) => session.adapter_id === adapter && !terminalStatuses.includes(session.status));
  const [viewedLimit, setViewedLimit] = useState("30");
  const [applicationLimit, setApplicationLimit] = useState("5");
  const [unlimitedViewed, setUnlimitedViewed] = useState(false);
  const [unlimitedApplications, setUnlimitedApplications] = useState(false);
  const [influence, setInfluence] = useState<Record<string, InfluenceLevel>>({ tasks: "medium", industry: "medium", skills: "medium" });
  const [message, setMessage] = useState("");
  const validLimit = (value: string, unlimited: boolean) =>
    unlimited || /^[1-9]\d*$/.test(value);
  const limitsAreValid =
    validLimit(viewedLimit, unlimitedViewed) &&
    validLimit(applicationLimit, unlimitedApplications);
  const create = useMutation({
    mutationFn: async () => {
      const session = await api<JobSession>("/sessions", {
        method: "POST",
        body: JSON.stringify({
          profile_id: profiles.data?.[0]?.id,
          adapter_id: adapter,
          viewed_limit: unlimitedViewed ? null : Number(viewedLimit),
          application_limit: unlimitedApplications ? null : Number(applicationLimit),
          minimum_scores: Object.fromEntries(Object.entries(influence).map(([key, level]) => [key, INFLUENCE_LEVELS.indexOf(level) + 1])),
        }),
      });
      await api(`/sessions/${session.id}/start`, { method: "POST" });
      return session;
    },
    onSuccess: (session) => {
      setMessage(
        session.adapter_id === "hh"
          ? `Сессия #${session.id} запущена. Откройте браузер и войдите в HH.ru.`
          : `Сессия #${session.id} запущена.`,
      );
      void qc.invalidateQueries({ queryKey: ["sessions"] });
      void qc.invalidateQueries({ queryKey: ["session-report"] });
    },
    onError: (error) => setMessage(error.message),
  });
  const action = async (sessionId: number, name: string) => {
    try {
      const response = await api<{ message?: string }>(
        `/sessions/${sessionId}/${name}`,
        { method: "POST" },
      );
      setMessage(
        response.message ||
          (name === "start" ? "Сессия запущена." : "Состояние сессии обновлено."),
      );
      await qc.invalidateQueries({ queryKey: ["sessions"] });
      await qc.invalidateQueries({ queryKey: ["session-report"] });
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Не удалось выполнить действие");
    }
  };
  const formatSessionLimit = (limit: number | null | undefined, legacyDefault: number): string =>
    limit === null ? "без ограничений" : String(limit ?? legacyDefault);
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
          <div className="row">
            <label>
              Сайт
              <select
                value={adapter}
                onChange={(e) => setAdapter(e.target.value)}
              >
                <option value="hh">HH.ru</option>
                {(adapters.data ?? []).filter((item) => item.site_id !== "hh").map((item) => <option value={item.site_id} key={item.site_id}>{item.display_name}</option>)}
              </select>
            </label>
          </div>
          <div className="row session-limits">
            <label>
              Просмотреть вакансий
              <input
                aria-label="Лимит просмотра вакансий"
                type="number"
                min="1"
                step="1"
                value={viewedLimit}
                disabled={unlimitedViewed}
                onChange={(e) => setViewedLimit(e.target.value)}
              />
              <small>После этого числа просмотренных вакансий сессия завершится.</small>
              <span className="checkline">
                <input aria-label="Без ограничений: просмотр вакансий" type="checkbox" checked={unlimitedViewed} onChange={(e) => setUnlimitedViewed(e.target.checked)} />
                Без ограничений
              </span>
            </label>
            <label>
              Лимит вакансий в работе
              <input
                aria-label="Лимит вакансий в работе"
                type="number"
                min="1"
                step="1"
                value={applicationLimit}
                disabled={unlimitedApplications}
                onChange={(e) => setApplicationLimit(e.target.value)}
              />
              <small>{adapter === "hirehi" ? "Считаются выбранные вакансии." : "Считаются отклики, подтверждённые выбранной площадкой."}</small>
              <span className="checkline">
                <input aria-label={adapter === "hirehi" ? "Без ограничений: выбранные вакансии" : "Без ограничений: отправка откликов"} type="checkbox" checked={unlimitedApplications} onChange={(e) => setUnlimitedApplications(e.target.checked)} />
                Без ограничений
              </span>
            </label>
          </div>
          <section className="influence-section" aria-labelledby="influence-heading">
            <h3 id="influence-heading">Влияние факторов на вакансии</h3>
            {INFLUENCE_CRITERIA.map((criterion) => {
              const selected = influence[criterion.key];
              const selectedIndex = INFLUENCE_LEVELS.indexOf(selected);
              return <div className="influence-control" key={criterion.key}>
                <div className="influence-control-head"><strong>{criterion.title}</strong><span className="tooltip-wrap"><button type="button" className="question-button" aria-label={`Подсказка: ${criterion.title}`} data-tooltip={criterion.hint}>?</button><span className="sr-only">{criterion.hint}</span></span></div>
                <input className="influence-range" type="range" min="1" max="3" step="1" value={selectedIndex + 1} aria-label={`Уровень влияния: ${criterion.title}`} aria-valuetext={["Низкий", "Средний", "Высокий"][selectedIndex]} onChange={(event) => setInfluence((current) => ({ ...current, [criterion.key]: INFLUENCE_LEVELS[Number(event.target.value) - 1] }))} />
                <div className="influence-levels" aria-hidden="true"><span>Низкий</span><span>Средний</span><span>Высокий</span></div>
              </div>;
            })}
          </section>
          <button
            onClick={() => create.mutate()}
            disabled={!profileReady || create.isPending || !limitsAreValid || blockedByAdapter}
          >
            {create.isPending ? "Запускаем…" : "Создать и запустить"}
          </button>
          {blockedByAdapter && <p className="notice" role="alert">Для выбранного сайта уже есть незавершённая сессия. Дождитесь её завершения.</p>}
          {!limitsAreValid && (
            <p className="notice error" role="alert">
              Введите целое положительное значение лимита или включите «Без ограничений».
            </p>
          )}
          {!profileReady && (
            <p className="notice">Сначала заполните профиль и выберите хотя бы одно резюме.</p>
          )}
        </article>
      {message && (
        <p className={`notice ${create.isError ? "error" : ""}`} role="status">
          {message}
        </p>
      )}
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

function SessionCard({ session, formatSessionLimit, action }: { session: JobSession; formatSessionLimit: (limit: number | null | undefined, legacyDefault: number) => string; action: (id: number, name: string) => Promise<void> }) {
  const report = useQuery({ queryKey: ["session-report", session.id], queryFn: () => api<{ ready: boolean; pdf_url: string | null }>(`/sessions/${session.id}/report`), enabled: session.adapter_id === "hirehi", retry: false, refetchInterval: (query) => query.state.data?.ready ? false : 2000 });
  const browserAvailable = session.adapter_id === "hh" || session.adapter_id === "hirehi";
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
              <p>
                {session.stop_reason ||
                  "Обработка вакансий идёт последовательно"}
              </p>
              <p className="session-limits-summary">
                Лимиты сессии: просмотр — {formatSessionLimit(session.viewed_limit, 30)}; {session.adapter_id === "hirehi" ? "выбрано" : "отправка"} — {formatSessionLimit(session.application_limit, 5)}.
              </p>
            </div>
            <div className="actions session-actions">
              {report.data?.ready && report.data.pdf_url && <a className="button-link session-report-button" aria-label={`Скачать PDF HireHi #${session.id}`} href={report.data.pdf_url} target="_blank" rel="noreferrer"><svg className="session-report-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M6 3.75h8.25L19 8.5v11.75H6z" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round"/><path d="M14 3.75V9h5M12.5 12v6m0 0-2.5-2.5m2.5 2.5 2.5-2.5" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"/></svg><span>Скачать PDF-отчёт</span></a>}
              {session.status === "CREATED" && (
                <>
                  <button onClick={() => void action(session.id, "start")}>Запустить</button>
                  {browserAvailable && (
                    <button className="secondary" onClick={() => void action(session.id, "browser")}>Открыть браузер</button>
                  )}
                </>
              )}
              {session.status === "WAITING_FOR_LOGIN" && (
                <>
                  <button className="secondary" onClick={() => void action(session.id, "browser")}>Открыть браузер</button>
                  <button className="secondary" onClick={() => void action(session.id, "browser/check")}>Проверить вход</button>
                  <button className="danger" onClick={() => void action(session.id, "stop")}>Остановить</button>
                </>
              )}
              {session.status === "RUNNING" && (
                <>
                  {browserAvailable && (
                    <button className="secondary" onClick={() => void action(session.id, "browser")}>Открыть браузер</button>
                  )}
                  <button className="danger" onClick={() => void action(session.id, "stop")}>Остановить</button>
                </>
              )}
              {session.status === "PAUSED" && (
                <>
                  <button onClick={() => void action(session.id, "resume")}>Продолжить</button>
                  <button className="danger" onClick={() => void action(session.id, "stop")}>Остановить</button>
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

function VacanciesPage() {
  const [offset, setOffset] = useState(0);
  const [allVacancies, setAllVacancies] = useState<Vacancy[]>([]);
  const q = useQuery({
    queryKey: ["vacancies", offset],
    queryFn: async () => {
      const response = await api<VacancyPage | Vacancy[]>(offset ? `/vacancies?limit=30&offset=${offset}` : "/vacancies");
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
      {allVacancies.length ? (
        <div className="vacancy-list">
          {allVacancies.map((v) => (
            <details className="panel vacancy-score" key={v.id}>
              <summary>
                <span className="vacancy-main">
                  <b>{v.title}</b>
                  <small>#{v.id} · {v.company || "Компания не указана"}</small>
                </span>
                <span className="score-total">
                  <strong>{v.evaluation?.score ?? "—"}</strong><small>/ 100</small>
                </span>
                <Status value={v.state} />
              </summary>
              {v.evaluation ? (
                <div className="score-details">
                  <p>{v.evaluation.reason}</p>
                  <div className="resume-score-heading">
                    <span className="eyebrow">РЕЛЕВАНТНОСТЬ ПО РЕЗЮМЕ</span>
                    <small>Итоговая оценка по выбранному резюме</small>
                  </div>
                  {presentationBreakdown(v.evaluation.score_breakdown ?? []).map((row) => (
                    <div className="score-row" key={row.key}>
                      <div><b>{row.title}</b><span>{row.max_points > 0 ? `${row.points} / ${row.max_points}` : "не применяется"}</span></div>
                      {row.max_points > 0 && <div className="scorebar"><i style={{ width: `${Math.min(100, Math.max(0, (row.points / row.max_points) * 100))}%` }} /></div>}
                      {typeof row.minimum_points === "number" ? (
                        <small>{row.minimum_failed ?? ((row.raw_points ?? row.points) < row.minimum_points) ? `Минимум: ${row.minimum_points} — не выполнен` : `Минимум: ${row.minimum_points} — выполнен`}</small>
                      ) : null}
                      <small>{row.explanation}</small>
                      {row.evidence?.length ? <em>{row.evidence.join(" · ")}</em> : null}
                    </div>
                  ))}
                  <a href={v.url} target="_blank" rel="noreferrer">Открыть вакансию на площадке</a>
                </div>
              ) : <p className="empty-score">Оценка ещё не завершена.</p>}
            </details>
          ))}
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
  const [loadingModels, setLoadingModels] = useState(false);
  const [saving, setSaving] = useState(false);
  useEffect(() => { if (settings.data) { setBaseUrl(settings.data.base_url || ""); setModel(settings.data.model || ""); } }, [settings.data]);
  const loadModels = async () => { setLoadingModels(true); setMessage(""); try { const result = await api<{ models: string[] }>("/model/models", { method: "POST", body: JSON.stringify({ base_url: baseUrl, ...(apiKey ? { api_key: apiKey } : {}) }) }); setModels(result.models); if (result.models.length) setModel(result.models[0]); setMessage(`Доступно моделей: ${result.models.length}`); } catch (error) { setMessage(error instanceof Error ? error.message : "Не удалось загрузить модели"); } finally { setLoadingModels(false); } };
  const save = async () => { setSaving(true); try { await api("/model/settings", { method: "PUT", body: JSON.stringify({ base_url: baseUrl, model, ...(apiKey ? { api_key: apiKey } : {}) }) }); setApiKey(""); setMessage("Настройки сохранены"); void qc.invalidateQueries({ queryKey: ["model-settings"] }); void qc.invalidateQueries({ queryKey: ["model-status"] }); } catch (error) { setMessage(error instanceof Error ? error.message : "Не удалось сохранить настройки"); } finally { setSaving(false); } };
  return (
    <section className="page">
      <Title
        eyebrow="ОБЛАЧНАЯ МОДЕЛЬ"
        note="Одна облачная модель обслуживает несколько строго типизированных ролей."
      >
        OpenAI API
      </Title>
      <article className="panel model">
        <div
          className={`orb ${q.data?.connected && q.data.model_available ? "online" : ""}`}
        ></div>
        <div>
          <Status
            value={q.data?.connected ? "Соединение есть" : "Нет соединения"}
          />
          <h2>{q.data?.model || "Модель не указана"}</h2>
          <p>
            {q.data?.model_available
              ? "Модель готова к structured outputs."
              : "Проверьте OpenAI API URL и ключ в конфигурации приложения."}
          </p>
          <code>Ключ хранится в защищённом хранилище DPAPI</code>
        </div>
        <button type="button" onClick={() => { void qc.invalidateQueries({ queryKey: ["model-status"] }); void qc.invalidateQueries({ queryKey: ["model-settings"] }); }}>
          Проверить снова
        </button>
      </article>
      <article className="panel model-settings">
        <label>Base URL (HTTPS или настроенный локальный gateway)<input value={baseUrl} onChange={(event) => { setBaseUrl(event.target.value); setModels([]); setMessage(""); }} placeholder="https://api.openai.com/v1" inputMode="url" /></label>
        <label>API-ключ<input type="password" value={apiKey} onChange={(event) => { setApiKey(event.target.value); setModels([]); setMessage(""); }} autoComplete="new-password" placeholder={settings.data?.has_api_key ? "Сохранённый ключ не отображается" : "Введите ключ"} /></label>
        {settings.data?.has_api_key && <small>Сохранён: {settings.data.masked_key}. Ключ не показывается.</small>}
        {models.length > 0 && <label>Модель<select value={model} onChange={(event) => setModel(event.target.value)}>{models.map((name) => <option key={name} value={name}>{name}</option>)}</select></label>}
        <div className="model-settings-actions"><button type="button" onClick={() => void loadModels()} disabled={!baseUrl || loadingModels}>{loadingModels ? "Загрузка…" : "Загрузить модели"}</button><button type="button" onClick={() => void save()} disabled={saving || !model || models.length === 0}>{saving ? "Сохранение…" : "Сохранить изменения"}</button></div>
        {message && <p role="status">{message}</p>}
      </article>
    </section>
  );
}

export default function App() {
  return (
    <Shell>
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/profile" element={<ProfilePage />} />
        <Route path="/sites" element={<SitesPage />} />
        <Route path="/session" element={<SessionPage />} />
        <Route path="/vacancies" element={<VacanciesPage />} />
        <Route path="/model" element={<ModelPage />} />
      </Routes>
    </Shell>
  );
}
