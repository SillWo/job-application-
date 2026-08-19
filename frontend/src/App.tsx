import { ReactNode, useEffect, useState } from "react";
import { NavLink, Route, Routes } from "react-router-dom";
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
  Report,
  Resume,
  Review,
  Vacancy,
  WorkExperience,
} from "./types";

const nav = [
  ["/", "Обзор"],
  ["/profile", "Профиль"],
  ["/sites", "Сайты"],
  ["/session", "Сессия"],
  ["/reviews", "Проверка"],
  ["/vacancies", "Вакансии"],
  ["/reports", "Отчёты"],
  ["/model", "Модель"],
];

function Shell({ children }: { children: ReactNode }) {
  const model = useQuery({
    queryKey: ["model"],
    queryFn: () =>
      api<{ connected: boolean; model_available: boolean; model: string }>(
        "/model/status",
      ),
  });
  return (
    <div className="shell">
      <aside className="sidebar" aria-label="Основная навигация">
        <div className="brand">
          <span className="brandmark">J</span>
          <div>
            <strong>Job Orchestrator</strong>
            <small>локальный агент</small>
          </div>
        </div>
        <nav>
          {nav.map(([to, label]) => (
            <NavLink key={to} to={to} end={to === "/"}>
              {label}
            </NavLink>
          ))}
        </nav>
        <div className="privacy">
          <span>Данные остаются локально</span>
          <small>Cookies и резюме не покидают компьютер</small>
        </div>
      </aside>
      <main>
        <header>
          <div>
            <span className={`dot ${model.data?.connected ? "ok" : ""}`}></span>
            {model.data?.connected ? model.data.model : "Ollama недоступна"}
          </div>
          <a href="/docs" target="_blank">
            API
          </a>
        </header>
        {children}
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
  const reviews = useQuery({
    queryKey: ["reviews"],
    queryFn: () => api<Review[]>("/reviews"),
  });
  const reports = useQuery({
    queryKey: ["reports"],
    queryFn: () => api<Report[]>("/reports"),
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
        <article>
          <small>Ручная проверка</small>
          <strong>
            {reviews.data?.filter((x) => x.status === "pending").length ?? "—"}
          </strong>
          <span>требуют решения</span>
        </article>
        <article>
          <small>Отчёты</small>
          <strong>{reports.data?.length ?? "—"}</strong>
          <span>локальных файлов</span>
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
          <h2>ИИ ищет и откликается</h2>
          <p>
            Ваш запрос на естественном языке учитывается при оценке каждой вакансии.
            Подходящие варианты проходят проверку перед откликом на HH.ru; CAPTCHA и вход
            в аккаунт передаются пользователю.
          </p>
          <div className="flow">
            <span>Запрос</span>
            <i>→</i>
            <span>Оценить</span>
            <i>→</i>
            <span>Проверить / откликнуться</span>
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
      <div className="row">
        <label>ФИО<input value={value.full_name ?? ""} onChange={(e) => change("full_name", e.target.value)} /></label>
        <label>Место проживания<input value={value.residence ?? ""} onChange={(e) => change("residence", e.target.value)} /></label>
      </div>
      <label>Где ищу работу<input value={value.job_search_locations.join(", ")} onChange={(e) => change("job_search_locations", e.target.value.split(",").map((item) => item.trim()).filter(Boolean))} placeholder="Города или направления через запятую" /></label>
      <div className="subsection">
        <div className="section-heading"><div><h3>Контакты</h3><small>Телефон, email и список мессенджеров.</small></div><button type="button" className="secondary" onClick={() => change("contacts", { ...value.contacts, messengers: [...value.contacts.messengers, ""] })}>+ Мессенджер</button></div>
        <div className="repeat-list">
          <div className="repeat-row"><label>Телефон<input aria-label="Телефон" value={value.contacts.phone ?? ""} onChange={(e) => change("contacts", { ...value.contacts, phone: e.target.value })} /></label><label>Email<input aria-label="Email" value={value.contacts.email ?? ""} onChange={(e) => change("contacts", { ...value.contacts, email: e.target.value })} /></label></div>
          {value.contacts.messengers.map((messenger, index) => <div className="repeat-row" key={index}><input aria-label={`Мессенджер ${index + 1}`} value={messenger} onChange={(e) => change("contacts", { ...value.contacts, messengers: value.contacts.messengers.map((item, itemIndex) => itemIndex === index ? e.target.value : item) })} placeholder="Telegram, WhatsApp или другой мессенджер" /><button type="button" className="icon-button" aria-label={`Удалить мессенджер ${index + 1}`} onClick={() => change("contacts", { ...value.contacts, messengers: value.contacts.messengers.filter((_, itemIndex) => itemIndex !== index) })}>×</button></div>)}
        </div>
      </div>
      <div className="subsection">
        <div className="section-heading"><div><h3>Образование</h3><small>Выберите тип для каждого учебного заведения.</small></div><button type="button" className="secondary" onClick={() => change("education", [...value.education, newEducation()])}>+ Образование</button></div>
        <div className="repeat-list">
          {value.education.map((item, index) => <div className="nested-card" key={index}>
            <div className="repeat-row"><select aria-label={`Тип образования ${index + 1}`} value={item.type} onChange={(e) => changeEducation(index, { type: e.target.value as EducationType })}>{educationOptions.map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select><button type="button" className="icon-button" aria-label={`Удалить образование ${index + 1}`} onClick={() => change("education", value.education.filter((_, itemIndex) => itemIndex !== index))}>×</button></div>
            <div className="row">
              <label>{item.type === "higher" ? "Университет" : "Учебное заведение"}<input value={item.institution} onChange={(e) => changeEducation(index, { institution: e.target.value })} /></label>
              {item.type !== "school" && <label>Факультет<input value={item.faculty ?? ""} onChange={(e) => changeEducation(index, { faculty: e.target.value })} /></label>}
            </div>
            {item.type === "higher" && <label>Степень<select value={item.degree ?? ""} onChange={(e) => changeEducation(index, { degree: (e.target.value || null) as Degree | null })}><option value="">Выберите степень</option><option value="bachelor">Бакалавр</option><option value="master">Магистр</option><option value="specialist">Специалист</option><option value="postgraduate">Аспирант</option></select></label>}
            {item.type !== "school" && <label>Специальность<input value={item.specialty ?? ""} onChange={(e) => changeEducation(index, { specialty: e.target.value })} /></label>}
            <div className="row"><label>Дата начала обучения<input type="month" value={item.start_date ?? ""} onChange={(e) => changeEducation(index, { start_date: e.target.value || null })} /></label><label>Дата окончания обучения<input type="month" value={item.end_date ?? ""} onChange={(e) => changeEducation(index, { end_date: e.target.value || null })} /></label></div>
          </div>)}
        </div>
      </div>
      <div className="subsection">
        <div className="section-heading"><div><h3>Языки</h3><small>Язык и уровень владения.</small></div><button type="button" className="secondary" onClick={() => change("languages", [...value.languages, { language: "", proficiency: "" }])}>+ Язык</button></div>
        <div className="repeat-list">{value.languages.map((language, index) => <div className="repeat-row" key={index}><input aria-label={`Язык ${index + 1}`} value={language.language} onChange={(e) => changeLanguage(index, { language: e.target.value })} placeholder="Например, английский" /><input aria-label={`Уровень языка ${index + 1}`} value={language.proficiency} onChange={(e) => changeLanguage(index, { proficiency: e.target.value })} placeholder="Например, B2" /><button type="button" className="icon-button" aria-label={`Удалить язык ${index + 1}`} onClick={() => change("languages", value.languages.filter((_, itemIndex) => itemIndex !== index))}>×</button></div>)}</div>
      </div>
      <label className="checkline"><input type="checkbox" checked={value.driver_license ?? false} onChange={(e) => change("driver_license", e.target.checked)} /> Есть водительские права</label>
      <div className="actions"><button type="button" onClick={onSave} disabled={saving}>{saving ? "Сохраняю…" : "Сохранить личный профиль"}</button></div>
    </article>
  );
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
    <label>Название резюме<input value={draft.name} onChange={(e) => update({ name: e.target.value })} placeholder="Например, Product Manager" /></label>
    <div className="row"><label>Предполагаемая должность<input value={draft.desired_title ?? ""} onChange={(e) => update({ desired_title: e.target.value })} /></label><label>Желаемый доход<input value={draft.desired_salary ?? ""} onChange={(e) => update({ desired_salary: e.target.value })} placeholder="Например, 180 000 ₽" /></label></div>
    <fieldset><legend>Желаемый тип занятости</legend><div className="option-grid">{employmentOptions.map(([key, label]) => <label className="checkline" key={key}><input type="checkbox" checked={draft.employment_types.includes(key)} onChange={() => toggleEmployment(key)} />{label}</label>)}</div></fieldset>
    <div className="subsection"><div className="section-heading"><div><h3>Формат работы</h3><small>Можно выбрать несколько вариантов.</small></div></div><div className="option-grid">{formatOptions.map(([key, label]) => <label className="checkline" key={key}><input type="checkbox" checked={draft.work_formats.includes(key)} onChange={() => update({ work_formats: draft.work_formats.includes(key) ? draft.work_formats.filter((item) => item !== key) : [...draft.work_formats, key] })} />{label}</label>)}</div></div>
    <label>Командировки<select value={draft.business_trips === null || draft.business_trips === undefined ? "" : draft.business_trips ? "can" : "cannot"} onChange={(e) => update({ business_trips: e.target.value === "" ? null : e.target.value === "can" })}><option value="">Выберите вариант</option><option value="can">Могу</option><option value="cannot">Не могу</option></select></label>
    <div className="subsection"><div className="section-heading"><div><h3>Опыт работы</h3><small>Добавьте должности и обязанности.</small></div><button type="button" className="secondary" onClick={() => update({ experiences: [...draft.experiences, newExperience()] })}>+ Опыт</button></div><div className="repeat-list">{draft.experiences.map((experience, index) => <div className="nested-card" key={index}><div className="repeat-row"><strong>Опыт #{index + 1}</strong><button type="button" className="icon-button" aria-label={`Удалить опыт ${index + 1}`} onClick={() => update({ experiences: draft.experiences.filter((_, itemIndex) => itemIndex !== index) })}>×</button></div><div className="row"><label>Компания<input value={experience.company} onChange={(e) => updateExperience(index, { company: e.target.value })} /></label><label>Должность<input value={experience.position} onChange={(e) => updateExperience(index, { position: e.target.value })} /></label></div><div className="row"><label>Начало работы<input type="month" value={experience.start_date ?? ""} onChange={(e) => updateExperience(index, { start_date: e.target.value || null })} /></label><label>Конец работы<input type="month" value={experience.end_date ?? ""} onChange={(e) => updateExperience(index, { end_date: e.target.value || null })} placeholder="Оставьте пустым, если работаете сейчас" /></label></div><label>Описание обязанностей<textarea value={experience.duties} onChange={(e) => updateExperience(index, { duties: e.target.value })} /></label></div>)}</div></div>
    <label>Навыки<small>Введите навыки через запятую — каждый станет отдельным тегом.</small><input value={skillsText} onChange={(e) => setSkillsText(e.target.value)} placeholder="CustDev, Scrum, аналитика" /></label>
    <div className="tag-list" aria-label="Навыки">{skillsText.split(",").map((skill) => skill.trim()).filter(Boolean).map((skill) => <span className="tag" key={skill}>{skill}</span>)}</div>
    <label>О себе<textarea className="about-text" value={draft.about} onChange={(e) => { const words = e.target.value.trim() ? e.target.value.trim().split(/\s+/u) : []; if (words.length <= 500) update({ about: e.target.value }); }} /><small className={wordCount > 500 ? "word-limit" : ""}>{wordCount} / 500 слов</small></label>
    <div className="actions"><button type="button" onClick={() => onSave({ ...draft, skills: skillsText.split(",").map((skill) => skill.trim()).filter(Boolean) })} disabled={saving}>{saving ? "Сохраняю…" : "Сохранить резюме"}</button><button type="button" className="secondary" onClick={onCancel}>Отмена</button></div>
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
  const sessionResumes = useQuery({
    queryKey: ["session-resumes", sessionProfile?.id],
    queryFn: () => api<Resume[]>(`/profiles/${sessionProfile?.id}/resumes`),
    enabled: Boolean(sessionProfile),
  });
  const sessions = useQuery({
    queryKey: ["sessions"],
    queryFn: () => api<JobSession[]>("/sessions"),
  });
  const current = sessions.data?.[0];
  const canCreate =
    !current || ["COMPLETED", "STOPPED", "FAILED"].includes(current.status);
  const profileReady = Boolean(
    sessionProfile &&
      sessionResumes.data?.some((resume) => resume.selected_for_matching),
  );
  const adapter = "hh";
  const mode = "autopilot";
  const [viewedLimit, setViewedLimit] = useState("30");
  const [applicationLimit, setApplicationLimit] = useState("5");
  const [unlimitedViewed, setUnlimitedViewed] = useState(false);
  const [unlimitedApplications, setUnlimitedApplications] = useState(false);
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
          mode,
          viewed_limit: unlimitedViewed ? null : Number(viewedLimit),
          application_limit: unlimitedApplications ? null : Number(applicationLimit),
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
    },
    onError: (error) => setMessage(error.message),
  });
  const action = async (name: string) => {
    if (!current) return;
    try {
      const response = await api<{ message?: string }>(
        `/sessions/${current.id}/${name}`,
        { method: "POST" },
      );
      setMessage(
        response.message ||
          (name === "start" ? "Сессия запущена." : "Состояние сессии обновлено."),
      );
      await qc.invalidateQueries({ queryKey: ["sessions"] });
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Не удалось выполнить действие");
    }
  };
  const counters = current?.counters ?? {};
  const formatSessionLimit = (limit: number | null | undefined, legacyDefault: number) =>
    limit === null ? "без ограничений" : (limit ?? legacyDefault);
  return (
    <section className="page">
      <Title
        eyebrow="АКТИВНАЯ СЕССИЯ"
        note="Состояние сохраняется после каждого значимого шага."
      >
        Наблюдайте, не гадайте
      </Title>
      {canCreate && (
        <article className="panel form">
          {current && <span className="eyebrow">НОВАЯ СЕССИЯ</span>}
          <div className="row">
            <label>
              Сайт
              <select
                value={adapter}
                disabled
              >
                <option value="hh">HH.ru</option>
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
              Отправить откликов
              <input
                aria-label="Лимит отправленных откликов"
                type="number"
                min="1"
                step="1"
                value={applicationLimit}
                disabled={unlimitedApplications}
                onChange={(e) => setApplicationLimit(e.target.value)}
              />
              <small>Считаются только отклики, подтверждённые HH.ru как отправленные.</small>
              <span className="checkline">
                <input aria-label="Без ограничений: отправка откликов" type="checkbox" checked={unlimitedApplications} onChange={(e) => setUnlimitedApplications(e.target.checked)} />
                Без ограничений
              </span>
            </label>
          </div>
          <button
            onClick={() => create.mutate()}
            disabled={
              !profileReady || create.isPending || !limitsAreValid
            }
          >
            {create.isPending ? "Запускаем…" : "Создать и запустить"}
          </button>
          {!limitsAreValid && (
            <p className="notice error" role="alert">
              Введите целое положительное значение лимита или включите «Без ограничений».
            </p>
          )}
          {!profileReady && (
            <p className="notice">Сначала заполните профиль и выберите хотя бы одно резюме.</p>
          )}
        </article>
      )}
      {message && (
        <p className={`notice ${create.isError ? "error" : ""}`} role="status">
          {message}
        </p>
      )}
      {current && (
        <>
          <article className="panel sessiontop">
            <div>
              <span className="eyebrow">
                СЕССИЯ #{current.id} · {current.adapter_id}
              </span>
              <h2>
                <Status value={current.status} />
              </h2>
              <p>
                {current.stop_reason ||
                  "Обработка вакансий идёт последовательно"}
              </p>
              <p className="session-limits-summary">
                Лимиты сессии: просмотр — {formatSessionLimit(current.viewed_limit, 30)}; отправка — {formatSessionLimit(current.application_limit, 5)}.
              </p>
            </div>
            <div className="actions">
              {current.status === "CREATED" && (
                <>
                  <button onClick={() => void action("start")}>Запустить</button>
                  {current.adapter_id === "hh" && (
                    <button className="secondary" onClick={() => void action("browser")}>Открыть браузер</button>
                  )}
                </>
              )}
              {current.status === "WAITING_FOR_LOGIN" && (
                <>
                  <button className="secondary" onClick={() => void action("browser")}>Открыть браузер</button>
                  <button className="secondary" onClick={() => void action("browser/check")}>Проверить вход</button>
                  <button className="danger" onClick={() => void action("stop")}>Остановить</button>
                </>
              )}
              {current.status === "RUNNING" && (
                <>
                  {current.adapter_id === "hh" && (
                    <button className="secondary" onClick={() => void action("browser")}>Открыть браузер</button>
                  )}
                  <button className="secondary" onClick={() => void action("pause")}>Пауза</button>
                  <button className="danger" onClick={() => void action("stop")}>Остановить</button>
                </>
              )}
              {current.status === "PAUSED" && (
                <>
                  <button onClick={() => void action("resume")}>Продолжить</button>
                  <button className="danger" onClick={() => void action("stop")}>Остановить</button>
                </>
              )}
            </div>
          </article>
          <div className="stats compact">
            {[
              ["Просмотрено", "viewed"],
              ["Отфильтровано", "filtered"],

              ["Отклики", "submitted"],
              ["Уже откликались", "already_applied"],
              ["Тестовые", "skipped_test"],
              ["Проверка", "review"],
              ["Ошибки", "errors"],
            ].map(([label, key]) => (
              <article key={key}>
                <small>{label}</small>
                <strong>{counters[key] ?? 0}</strong>
              </article>
            ))}
          </div>
        </>
      )}
    </section>
  );
}

function ReviewsPage() {
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["reviews"],
    queryFn: () => api<Review[]>("/reviews"),
  });
  const decide = (id: number, decision: string) =>
    api(`/reviews/${id}/${decision}`, {
      method: "POST",
      body: JSON.stringify({ answer: "" }),
    }).then(() => qc.invalidateQueries({ queryKey: ["reviews"] }));
  return (
    <section className="page">
      <Title
        eyebrow="РУЧНАЯ ПРОВЕРКА"
        note="Автоматизация остановилась там, где нужен ваш контекст."
      >
        Решения, которые нельзя выдумывать
      </Title>
      {q.data?.length ? (
        q.data.map((r) => (
          <article className="panel review" key={r.id}>
            <div>
              <Status value={r.kind} />
              <h2>{r.question}</h2>
              <small>
                Сессия #{r.session_id} · вакансия #{r.vacancy_id}
              </small>
            </div>
            {r.status === "pending" ? (
              <div className="actions">
              <button onClick={() => decide(r.id, "approve")}>
                Подтвердить отклик
                </button>
                <button
                  className="secondary"
                  onClick={() => decide(r.id, "reject")}
                >
                  Пропустить
                </button>
              </div>
            ) : (
              <Status value={r.status} />
            )}
          </article>
        ))
      ) : (
        <Empty>Неизвестных вопросов, CAPTCHA и других блокеров пока нет.</Empty>
      )}
    </section>
  );
}

function VacanciesPage() {
  const q = useQuery({
    queryKey: ["vacancies"],
    queryFn: () => api<Vacancy[]>("/vacancies"),
  });
  return (
    <section className="page">
      <Title
        eyebrow="ВАКАНСИИ"
        note="Полная история решений хранится в локальной SQLite."
      >
        Каждое решение объяснимо
      </Title>
      {q.data?.length ? (
        <div className="vacancy-list">
          {q.data.map((v) => (
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
                  {v.evaluation.score_breakdown?.map((row) => (
                    row.key === "green_flags" ? null : (
                    <div className="score-row" key={row.key}>
                      <div><b>{row.title}</b><span>{row.max_points > 0 ? `${row.points} / ${row.max_points}` : "не применяется"}</span></div>
                      {row.max_points > 0 && <div className="scorebar"><i style={{ width: `${Math.min(100, Math.max(0, (row.points / row.max_points) * 100))}%` }} /></div>}
                      <small>{row.explanation}</small>
                      {row.evidence?.length ? <em>{row.evidence.join(" · ")}</em> : null}
                    </div>
                    )
                  ))}
                  <a href={v.url} target="_blank" rel="noreferrer">Открыть вакансию на HH.ru</a>
                </div>
              ) : <p className="empty-score">Оценка ещё не завершена.</p>}
            </details>
          ))}
        </div>
      ) : (
        <Empty>
          Запустите сессию HH.ru, чтобы увидеть найденные вакансии.
        </Empty>
      )}
    </section>
  );
}

function ReportsPage() {
  const q = useQuery({
    queryKey: ["reports"],
    queryFn: () => api<Report[]>("/reports"),
  });
  return (
    <section className="page">
      <Title
        eyebrow="ОТЧЁТЫ"
        note="Читаемый PDF создаётся локально после каждой завершённой сессии и содержит полную оценку всех вакансий."
      >
        Итоги без внешних сервисов
      </Title>
      {q.isPending ? (
        <div className="empty" role="status"><b>Загружаем отчёты</b><p>Проверяем локальные результаты завершённых сессий.</p></div>
      ) : q.isError ? (
        <div className="empty error-state" role="alert"><b>Не удалось загрузить отчёты</b><p>{q.error.message}</p></div>
      ) : q.data?.length ? (
        q.data.map((r) => (
          <article className="panel report-card" key={r.id}>
            <div className="report-heading">
              <div>
                <span className="eyebrow">ОТЧЁТ #{r.id}</span>
                <h2>Сессия #{r.session_id}</h2>
                <small>{new Date(r.created_at).toLocaleString("ru")}</small>
              </div>
              <Status value={r.summary.status || "ЗАВЕРШЕНА"} />
            </div>
            <p>{r.summary.stop_reason || "Причина завершения не указана"}</p>
            <div className="report-metrics">
              {[
                ["Вакансий", r.summary.aggregates?.total ?? r.summary.vacancies?.length ?? 0],
                ["Оценено", r.summary.aggregates?.evaluated ?? 0],
                ["Подходит", r.summary.aggregates?.matched ?? r.summary.counters?.matched ?? 0],
                ["Отправлено", r.summary.aggregates?.submitted ?? r.summary.counters?.submitted ?? 0],
                ["Ошибки", r.summary.aggregates?.errors ?? r.summary.counters?.errors ?? 0],
              ].map(([label, value]) => <span key={String(label)}><b>{value}</b><small>{label}</small></span>)}
            </div>
            <a className="button-link" href={r.pdf_url} download>
              Скачать PDF
            </a>
          </article>
        ))
      ) : (
        <Empty>Отчёт появится после завершения первой сессии.</Empty>
      )}
    </section>
  );
}

function ModelPage() {
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["model"],
    queryFn: () =>
      api<{
        connected: boolean;
        model_available: boolean;
        model: string;
        provider: string;
        message?: string;
      }>("/model/status"),
  });
  return (
    <section className="page">
      <Title
        eyebrow="ЛОКАЛЬНАЯ МОДЕЛЬ"
        note="Одна физическая модель обслуживает несколько строго типизированных ролей."
      >
        Ollama на вашем компьютере
      </Title>
      <article className="panel model">
        <div
          className={`orb ${q.data?.connected && q.data.model_available ? "online" : ""}`}
        ></div>
        <div>
          <Status
            value={q.data?.connected ? "Соединение есть" : "Нет соединения"}
          />
          <h2>{q.data?.model || "qwen3.5:9b-q4_K_M"}</h2>
          <p>
            {q.data?.model_available
              ? "Модель готова к structured outputs."
              : "Запустите Ollama и загрузите модель командой ниже."}
          </p>
          <code>ollama pull qwen3.5:9b-q4_K_M</code>
        </div>
        <button onClick={() => qc.invalidateQueries({ queryKey: ["model"] })}>
          Проверить снова
        </button>
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
        <Route path="/reviews" element={<ReviewsPage />} />
        <Route path="/vacancies" element={<VacanciesPage />} />
        <Route path="/reports" element={<ReportsPage />} />
        <Route path="/model" element={<ModelPage />} />
      </Routes>
    </Shell>
  );
}
