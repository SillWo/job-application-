import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import App from "./App";
import { completionStatusFromCoverage, previewSections, resumeCompletionStatus, resumeContactLabel, resumeGenderLabel, safeCompletionErrorMessage, safeResumeImportUrl, sourceRecordFromResponse } from "./resumeSources";

const preview = { source_site: "hh", target_title: "Product Manager", questions: [] };
const genderPreview = { ...preview, questions: [{ id: "grammatical_gender", question: "Какой род использовать в сопроводительных письмах?", options: ["male", "female"], required: true }] };
const sites = [["hh", "HH.ru"], ["hirehi", "HireHi"], ["zarplata", "Zarplata.ru"]] as const;
const saved = (status: "valid" | "changed" | "unavailable" = "valid", token = "saved-token", sourcePreview: unknown = preview, grammaticalGender?: "male" | "female", adapterId = "hh", resumeDataStatus: "ready" | "missing" | "corrupt" = "ready") => ({ adapter_id: adapterId, status, checked_at: "2026-09-14T10:00:00Z", uses_saved_data: true, resume_data_status: resumeDataStatus, resume_data_saved_at: resumeDataStatus === "ready" ? "2026-09-14T10:00:00Z" : null, resume_data_error_message: null, preview_token: status === "unavailable" ? undefined : token, preview: sourcePreview, changed: status === "changed", ...(grammaticalGender ? { grammatical_gender: grammaticalGender } : {}) });
type Call = { url: string; init?: RequestInit };

function mount(path: string, client = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  return { ...render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[path]}><App /></MemoryRouter></QueryClientProvider>), client };
}
async function readyInput(label: string) {
  const input = await screen.findByLabelText(label);
  await waitFor(() => expect(input).toBeEnabled());
  return input;
}
function response(body: unknown, ok = true) { return { ok, status: ok ? 200 : 422, json: async () => body }; }
async function chooseOption(label: string, option: string) {
  fireEvent.click(await screen.findByRole("combobox", { name: label }));
  const item = await screen.findByRole("option", { name: option });
  fireEvent.pointerDown(item, { pointerType: "mouse", button: 0 });
  fireEvent.mouseUp(item);
  fireEvent.click(item, { detail: 1 });
}
function installServer(options: { sources?: unknown[]; confirm?: unknown; confirmResult?: unknown; confirmOk?: boolean; refresh?: unknown; refreshOk?: boolean; refreshResult?: Promise<unknown>; create?: unknown; createOk?: boolean; preview?: unknown; previewToken?: string; previewOk?: boolean; previewResult?: Promise<unknown>; deleteOk?: boolean } = {}) {
  const calls: Call[] = [];
  let sources = options.sources ?? [];
  let previewRequest: { adapter_id?: string; resume_url?: string } = {};
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input); calls.push({ url, init });
    if (url.endsWith("/api/resume-sources") && !init?.method) return response(sources);
    if (url.endsWith("/api/resume-sources/preview")) { previewRequest = JSON.parse(String(init?.body ?? "{}")); if (options.previewResult) return options.previewResult.then((result) => response(result)); const responsePreview = options.preview ?? { ...preview, source_site: previewRequest.adapter_id }; return response({ preview_token: options.previewToken ?? "preview-token-12345678901234567890", preview: responsePreview }, options.previewOk !== false); }
    if (url.endsWith("/api/resume-sources/confirm")) { const body = JSON.parse(String(init?.body ?? "{}")); const confirmed = options.confirmResult ?? options.confirm ?? { ...saved("valid", "saved-token", options.preview ?? { ...preview, source_site: body.adapter_id }, body.grammatical_gender, body.adapter_id), source_url: previewRequest.resume_url }; if (options.confirmOk !== false) { const adapterId = body.adapter_id; sources = [...sources.filter((item) => (item as { adapter_id?: string }).adapter_id !== adapterId), confirmed]; } return response(confirmed, options.confirmOk !== false); }
    const refreshAdapter = url.match(/\/api\/resume-sources\/(hh|hirehi|zarplata)\/refresh$/u)?.[1];
    if (refreshAdapter) return options.refreshResult ? options.refreshResult.then((result) => response(result)) : response(options.refresh ?? saved("valid", "fresh-token", preview, undefined, refreshAdapter), options.refreshOk !== false);
    if (url.endsWith("/api/resume-sources/hh") && init?.method === "PATCH") { const body = JSON.parse(String(init.body ?? "{}")); const current = sources[0] as Record<string, unknown> | undefined; sources = [saved("valid", "saved-token", current?.preview ?? preview, body.grammatical_gender)]; return response(sources[0]); }
    const deleteAdapter = url.match(/\/api\/resume-sources\/(hh|hirehi|zarplata)$/u)?.[1];
    if (deleteAdapter && init?.method === "DELETE") { if (options.deleteOk !== false) sources = sources.filter((item) => (item as { adapter_id?: string }).adapter_id !== deleteAdapter); return response({}, options.deleteOk !== false); }
    if (url.endsWith("/api/session-draft")) return response({ revision: 1, draft: null });
    if (url.endsWith("/api/sessions") && init?.method === "POST") return response(options.create ?? { id: 7, adapter_id: "hh", status: "CREATED", counters: {} }, options.createOk !== false);
    if (url.endsWith("/api/sessions")) return response([]);
    if (url.endsWith("/api/notifications")) return response([]);
    return response({});
  }));
  return { calls, getSources: () => sources };
}

beforeEach(() => { sessionStorage.clear(); localStorage.clear(); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

test("renders normalized resume sections and contact keys with Russian labels", () => {
  expect(previewSections({ sections: [{ key: "about" }, { key: "experience" }, { key: "skills" }, { key: "contacts" }, { key: "internal_payload" }] }).map((item) => item.label)).toEqual([
    "О себе", "Опыт работы", "Навыки", "Контакты", "Другие сведения",
  ]);
  expect(resumeContactLabel("email")).toBe("Электронная почта");
  expect(resumeContactLabel("phone")).toBe("Телефон");
  expect(resumeContactLabel("custom_contact")).toBe("Другой контакт");
  expect(resumeGenderLabel("female")).toBe("Женский");
  expect(sourceRecordFromResponse({ ...saved(), source_url: "https://hh.ru/resume/public-123" })?.sourceUrl).toBe("https://hh.ru/resume/public-123");
  expect(sourceRecordFromResponse({ ...saved(), source_url: "https://evil.test/resume/private" })?.sourceUrl).toBeUndefined();
  expect(safeResumeImportUrl("https://hh.ru/resume/public-123?print=true", "hh", "https://hh.ru/resume/public-123")).toBe("https://hh.ru/resume/public-123?print=true");
  expect(safeResumeImportUrl("https://hh.ru/resume/public-123?token=private", "hh", "https://hh.ru/resume/public-123")).toBeUndefined();
});

test("derives completion from every required main section and ignores optional sections", () => {
  const mainSections = ["identity", "contacts", "target", "location", "experience", "skills", "education", "languages", "about", "total_experience"];
  const full = [...mainSections, "portfolio", "certificates", "recommendations"].map((key) => ({ key, status: "present" }));
  expect(completionStatusFromCoverage({ sections: full }, "ready")).toBe("complete");
  expect(completionStatusFromCoverage({ coverage: { sections: mainSections } }, "ready")).toBe("complete");
  expect(completionStatusFromCoverage({ coverage: { sections: mainSections, missing: ["skills"] } }, "ready")).toBe("partial");
  expect(completionStatusFromCoverage({ sections: full.filter((section) => section.key !== "skills") }, "ready")).toBe("partial");
  expect(completionStatusFromCoverage({ sections: [...full, { key: "experience[0].company", status: "parse_error" }] }, "ready")).toBe("partial");
  expect(completionStatusFromCoverage({}, "missing")).toBe("error");
  expect(resumeCompletionStatus(sourceRecordFromResponse({ ...saved(), completion_status: "complete", resume_data_status: "corrupt" }) ?? undefined)).toBe("error");
  expect(safeCompletionErrorMessage("Ошибка https://private.example/resume/secret?token=x")).not.toContain("private.example");
});

test("renders all completion labels and semantic tones on profile cards", async () => {
  const server = installServer({ sources: [
    { ...saved("valid", "one", { sections: [] }), completion_status: "complete", adapter_id: "hh" },
    { ...saved("valid", "two", { sections: [] }, undefined, "hirehi"), completion_status: "partial", adapter_id: "hirehi" },
    { ...saved("valid", "three", { sections: [] }, undefined, "zarplata", "corrupt"), completion_status: "error", completion_error_message: "Не удалось прочитать https://private.example/resume/secret?token=x", adapter_id: "zarplata" },
  ] });
  mount("/profile");
  await readyInput("Ссылка на резюме на HH.ru");
  const cards = await screen.findAllByRole("article");
  expect(within(cards[0]).getByRole("status", { name: "Заполнено" })).toHaveAttribute("data-tone", "success");
  expect(within(cards[1]).getByRole("status", { name: "Частично" })).toHaveAttribute("data-tone", "warning");
  expect(within(cards[2]).getByRole("status", { name: "Ошибка" })).toHaveAttribute("data-tone", "danger");
  const reason = within(cards[2]).getByRole("button", { name: "Причина ошибки для Zarplata.ru" });
  fireEvent.mouseEnter(reason.parentElement as HTMLElement);
  expect(within(cards[2]).getByRole("tooltip")).toHaveTextContent("ссылку");
  expect(within(cards[2]).getByRole("tooltip")).not.toHaveTextContent("private.example");
  fireEvent.mouseLeave(reason.parentElement as HTMLElement);
  fireEvent.focus(reason);
  expect(within(cards[2]).getByRole("tooltip")).toBeVisible();
  expect(server.calls.filter((call) => call.url.endsWith("/api/resume-sources") && !call.init?.method)).toHaveLength(1);
});

test("a site without a saved record is shown as not filled", async () => {
  installServer();
  mount("/profile");
  await readyInput("Ссылка на резюме на HH.ru");
  const card = (await screen.findByRole("heading", { name: "HH.ru" })).closest("article") as HTMLElement;
  expect(within(card).getByRole("status", { name: "Не заполнено" })).toHaveAttribute("data-tone", "neutral");
});

test("loads saved sources from server after remount and purges legacy storage", async () => {
  sessionStorage.setItem("job-orchestrator.resume-sources", JSON.stringify({ hh: { previewToken: "secret-token", resume_url: "https://hh.ru/resume/secret" } }));
  localStorage.setItem("job-orchestrator.resume-sources", JSON.stringify({ hh: { previewToken: "legacy-token" } }));
  sessionStorage.setItem("keep-session-state", "keep");
  localStorage.setItem("keep-local-state", "keep");
  const server = installServer({ sources: [saved()] });
  const first = mount("/profile");
  expect(await screen.findByRole("heading", { name: "HH.ru" })).toBeInTheDocument();
  await waitFor(() => expect(server.calls.filter((call) => call.url.endsWith("/api/resume-sources") && !call.init?.method)).toHaveLength(1));
  expect(sessionStorage.getItem("job-orchestrator.resume-sources")).toBeNull();
  expect(localStorage.getItem("job-orchestrator.resume-sources")).toBeNull();
  first.unmount();
  mount("/profile", new QueryClient({ defaultOptions: { queries: { retry: false } } }));
  await waitFor(() => expect(server.calls.filter((call) => call.url.endsWith("/api/resume-sources") && !call.init?.method)).toHaveLength(2));
  expect(sessionStorage.getItem("keep-session-state")).toBe("keep");
  expect(localStorage.getItem("keep-local-state")).toBe("keep");
});

test("prefills the editable resume URL only from a safe allowlisted source URL", async () => {
  const sourceUrl = "https://hh.ru/resume/public-resume-123";
  installServer({ sources: [{ adapter_id: "hh", status: "valid", checked_at: "2026-09-14T10:00:00Z", source_url: sourceUrl, preview: preview }] });
  mount("/profile");
  const input = await readyInput("Ссылка на резюме на HH.ru");
  await waitFor(() => expect(input).toHaveValue(sourceUrl));
  expect(screen.queryByRole("link", { name: sourceUrl })).not.toBeInTheDocument();
});

test("profile URL typing, blur and Enter do not start an operation", async () => {
  const server = installServer({ sources: [saved()] }); mount("/profile");
  const input = await readyInput("Ссылка на резюме на HH.ru");
  const card = within(input.closest("article") as HTMLElement);
  expect(input).toHaveAttribute("readonly");
  fireEvent.click(card.getByRole("button", { name: "Редактировать ссылку на HH.ru" }));
  fireEvent.change(input, { target: { value: "https://hh.ru/resume/abc123" } });
  fireEvent.blur(input);
  fireEvent.keyDown(input, { key: "Enter" });
  expect(input).toHaveValue("https://hh.ru/resume/abc123");
  expect(card.getByRole("button", { name: "Сохранить изменения" })).toBeInTheDocument();
  expect(card.getByRole("button", { name: "Отменить редактирование ссылки на HH.ru" })).toBeInTheDocument();
  expect(card.getByRole("button", { name: "Удалить резюме для HH.ru" })).toBeInTheDocument();
  expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  await waitFor(() => expect(server.calls.filter((call) => call.url.endsWith("/api/resume-sources"))).toHaveLength(1));
  expect(server.calls.some((call) => call.url.includes("/preview") || call.url.includes("/confirm") || call.url.includes("/refresh") || call.init?.method === "PATCH" || call.init?.method === "DELETE")).toBe(false);
});

test("renders saved preview in a collapsed details list with present and missing icons", async () => {
  installServer({ sources: [saved("valid", "token", { sections: [{ key: "about", status: "present" }, { key: "experience", status: "not_provided" }], contacts: { found: ["email"], hidden: ["phone"] } })] });
  mount("/profile");
  const card = screen.getByRole("heading", { name: "HH.ru" }).closest("article") as HTMLElement;
  await within(card).findByRole("img", { name: "О себе: Найдено" });
  const details = card.querySelector("details.resume-coverage") as HTMLDetailsElement;
  expect(details).toBeInTheDocument();
  expect(details.open).toBe(false);
  fireEvent.click(details.querySelector("summary") as HTMLElement);
  expect(details.open).toBe(true);
  expect(within(details).getByRole("img", { name: "О себе: Найдено" })).toHaveAttribute("data-tone", "success");
  expect(within(details).getByRole("img", { name: "Опыт работы: Не указано" })).toHaveAttribute("data-tone", "danger");
  expect(within(details).getByRole("img", { name: "Электронная почта: Найдено" })).toHaveAttribute("data-tone", "success");
  expect(within(details).getByRole("img", { name: "Телефон: Скрыто на сайте" })).toHaveAttribute("data-tone", "danger");
  expect(within(details).queryByText("Найдено")).not.toBeInTheDocument();
});

test("shows an empty expandable list for a saved empty preview", async () => {
  installServer({ sources: [{ adapter_id: "hh", status: "valid", preview: {} }] });
  mount("/profile");
  const card = screen.getByRole("heading", { name: "HH.ru" }).closest("article") as HTMLElement;
  await waitFor(() => expect(card.querySelector("details.resume-coverage")).toBeInTheDocument());
  const details = card.querySelector("details.resume-coverage") as HTMLDetailsElement;
  expect(details).toBeInTheDocument();
  fireEvent.click(details.querySelector("summary") as HTMLElement);
  expect(within(details).getByText("Платформа не передала разделы или контакты.")).toBeVisible();
});

test("session sends one auto-start launch request with an idempotency key and keeps source after success", async () => {
  const server = installServer({ sources: [saved()] }); mount("/session");
  const launch = await screen.findByRole("button", { name: "Создать и запустить" }); await waitFor(() => expect(launch).toBeEnabled()); fireEvent.click(launch);
  await waitFor(() => expect(server.calls.some((call) => call.url.endsWith("/api/sessions") && call.init?.method === "POST")).toBe(true));
  const createIndex = server.calls.findIndex((call) => call.url.endsWith("/api/sessions") && call.init?.method === "POST");
  expect(server.calls.filter((call) => call.url.endsWith("/api/sessions") && call.init?.method === "POST")).toHaveLength(1);
  expect(server.calls.some((call) => call.url.endsWith("/api/resume-sources/hh/refresh"))).toBe(false);
  expect(JSON.parse(String(server.calls[createIndex].init?.body))).not.toHaveProperty("resume_preview_token");
  expect(JSON.parse(String(server.calls[createIndex].init?.body))).not.toHaveProperty("resume_consent");
  expect(JSON.parse(String(server.calls[createIndex].init?.body))).toMatchObject({ auto_start: true });
  expect(server.calls[createIndex].init?.headers).toEqual(expect.objectContaining({ "Idempotency-Key": expect.any(String) }));
  expect(document.querySelector(".session-resume-requirement")).not.toBeInTheDocument();
});

test("saved source survives launch, stop, and navigation between session and profile", async () => {
  const calls: Call[] = [];
  let session: Record<string, unknown> | null = null;
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input); calls.push({ url, init });
    if (url.endsWith("/api/resume-sources") && !init?.method) return response([saved()]);
    if (url.endsWith("/api/session-draft")) return response({ revision: 1, draft: null });
    if (url.endsWith("/api/sessions") && init?.method === "POST") {
      session = { id: 7, adapter_id: "hh", status: "PREPARING", counters: {} };
      return response(session);
    }
    if (url.endsWith("/api/sessions/7/stop")) { if (session) session.status = "STOPPED"; return response({}); }
    if (url.endsWith("/api/sessions")) return response(session ? [session] : []);
    if (url.endsWith("/api/notifications")) return response([]);
    return response({});
  }));
  mount("/session");
  const launch = await screen.findByRole("button", { name: "Создать и запустить" });
  await waitFor(() => expect(launch).toBeEnabled()); fireEvent.click(launch);
  await waitFor(() => expect(screen.getByRole("button", { name: "Создать и запустить" })).toBeInTheDocument());
  const stop = await screen.findByRole("button", { name: "Остановить" }); fireEvent.click(stop);
  await waitFor(() => expect(calls.some((call) => call.url.endsWith("/api/sessions/7/stop"))).toBe(true));
  fireEvent.click(screen.getAllByRole("link", { name: "Профиль" })[0]);
  expect(await screen.findByRole("heading", { name: "HH.ru" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("link", { name: "Сессия" }));
  expect(await screen.findByRole("button", { name: "Создать и запустить" })).toBeInTheDocument();
  expect(document.querySelector(".session-resume-requirement")).not.toBeInTheDocument();
  expect(calls.some((call) => call.init?.method === "DELETE")).toBe(false);
});

test("launch error leaves the saved source visible", async () => {
  const server = installServer({ sources: [saved("unavailable")], createOk: false }); mount("/session");
  const launch = await screen.findByRole("button", { name: "Создать и запустить" }); await waitFor(() => expect(launch).toBeEnabled()); fireEvent.click(launch);
  await waitFor(() => expect(server.calls.filter((call) => call.url.endsWith("/api/sessions") && call.init?.method === "POST")).toHaveLength(1));
  expect(document.querySelector(".session-resume-requirement")).not.toBeInTheDocument();
});

test("keeps profile gender normalization as helper behavior without a profile editor", () => {
  expect(resumeGenderLabel("female")).toBe("Женский");
  expect(resumeGenderLabel("male")).toBe("Мужской");
});

test("keeps the saved source visible when auto-start creation fails", async () => {
  const server = installServer({ sources: [saved()], createOk: false });
  mount("/session", new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retryDelay: 0 } } }));
  const launch = await screen.findByRole("button", { name: "Создать и запустить" });
  await waitFor(() => expect(launch).toBeEnabled());
  fireEvent.click(launch);
  await waitFor(() => expect(server.calls.some((call) => call.url.endsWith("/api/sessions") && call.init?.method === "POST")).toBe(true));
  expect(await screen.findByRole("alert")).toHaveTextContent("Ошибка запроса");
  expect(server.calls.filter((call) => call.url.endsWith("/api/sessions") && call.init?.method === "POST")).toHaveLength(2);
  expect(document.querySelector(".session-resume-requirement")).not.toBeInTheDocument();
});

test.each([
  ["hh", "HH.ru", "https://hh.ru/resume/hh-public"],
  ["hirehi", "HireHi", "https://hirehi.ru/resume/hirehi-public"],
  ["zarplata", "Zarplata.ru", "https://zarplata.ru/resume/zarplata-public"],
] as const)("shows a safe saved URL and received data for %s", async (adapterId, siteLabel, sourceUrl) => {
  installServer({ sources: [{ ...saved("valid", "saved-token", { sections: [{ key: "about", status: "present" }] }, undefined, adapterId), source_url: sourceUrl }] });
  mount("/profile");
  const card = (await screen.findByRole("heading", { name: siteLabel })).closest("article") as HTMLElement;
  const input = within(card).getByLabelText(`Ссылка на резюме на ${siteLabel}`);
  await waitFor(() => expect(input).toHaveValue(sourceUrl));
  const details = card.querySelector("details.resume-coverage") as HTMLDetailsElement;
  expect(details).toBeInTheDocument();
  expect(details.open).toBe(false);
  fireEvent.click(details.querySelector("summary") as HTMLElement);
  expect(within(details).getByRole("img", { name: "О себе: Найдено" })).toHaveAttribute("data-tone", "success");
});

test("URL typing, blur and Enter cause no preview, confirm, refresh, or patch request", async () => {
  const { calls } = installServer();
  mount("/profile");
  const input = await readyInput("Ссылка на резюме на HH.ru");
  const card = within(input.closest("article") as HTMLElement);
  fireEvent.change(input, { target: { value: "https://hh.ru/resume/draft" } });
  fireEvent.blur(input);
  fireEvent.keyDown(input, { key: "Enter" });
  expect(input).toHaveValue("https://hh.ru/resume/draft");
  expect(card.getByRole("button", { name: "Сохранить ссылку" })).toBeInTheDocument();
  expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  await waitFor(() => expect(calls.filter((call) => call.url.endsWith("/api/resume-sources"))).toHaveLength(1));
  expect(calls.some((call) => call.url.includes("/preview") || call.url.includes("/confirm") || call.url.includes("/refresh") || ["PATCH", "DELETE"].includes(call.init?.method ?? ""))).toBe(false);
});

test("failed first extraction shows error through the session route and clearing the draft resets it", async () => {
  const server = installServer({ previewOk: false });
  mount("/profile");
  const input = await readyInput("Ссылка на резюме на HH.ru");
  fireEvent.change(input, { target: { value: "https://hh.ru/resume/public-1" } });
  fireEvent.click(within(input.closest("article") as HTMLElement).getByRole("button", { name: "Сохранить ссылку" }));
  expect(await screen.findByRole("status", { name: "Ошибка" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("link", { name: "Сессия" }));
  const launch = await screen.findByRole("button", { name: "Создать и запустить" });
  expect(launch).toBeEnabled();
  fireEvent.click(launch);
  expect(await screen.findByText('Для сайта HH.ru не удалось извлечь необходимые данные из резюме, проверьте раздел "Профиль"')).toBeInTheDocument();
  expect(server.calls.some((call) => call.url.endsWith("/api/sessions") && call.init?.method === "POST")).toBe(false);
  fireEvent.click(screen.getAllByRole("link", { name: "Профиль" })[0]);
  const emptyInput = await readyInput("Ссылка на резюме на HH.ru");
  expect(emptyInput).toHaveValue("");
  fireEvent.click(screen.getByRole("button", { name: "Сбросить ошибку импорта для HH.ru" }));
  expect(await within(emptyInput.closest("article") as HTMLElement).findByRole("status", { name: "Не заполнено" })).toBeInTheDocument();
  expect(server.calls.some((call) => call.url.endsWith("/api/resume-sources/hh") && call.init?.method === "DELETE")).toBe(false);
});

test("starting an existing session checks the source for that session's own site", async () => {
  const server = installServer({ sources: [saved("valid", "saved-token", preview, undefined, "hh") ] });
  const originalFetch = globalThis.fetch as unknown as ReturnType<typeof vi.fn>;
  originalFetch.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input); server.calls.push({ url, init });
    if (url.endsWith("/api/resume-sources") && !init?.method) return response([saved("valid", "saved-token", preview, undefined, "hh")]);
    if (url.endsWith("/api/session-draft")) return response({ revision: 1, draft: null });
    if (url.endsWith("/api/sessions")) return response([{ id: 9, adapter_id: "hirehi", status: "CREATED", counters: {} }]);
    if (url.endsWith("/api/notifications")) return response([]);
    return response({});
  });
  mount("/session");
  const start = await screen.findByRole("button", { name: "Запустить" });
  fireEvent.click(start);
  expect(await screen.findByText('Для сайта HireHi не загружено резюме, проверьте раздел "Профиль"')).toBeInTheDocument();
  expect(server.calls.some((call) => call.url.endsWith("/api/sessions/9/start"))).toBe(false);
});

test("initial source loading locks profile fields and actions until the saved state is known", async () => {
  let finishLoad!: (result: unknown) => void;
  const calls: Call[] = [];
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input); calls.push({ url, init });
    if (url.endsWith("/api/resume-sources") && !init?.method) return new Promise((resolve) => { finishLoad = (result) => resolve(response(result)); });
    if (url.endsWith("/api/notifications")) return Promise.resolve(response([]));
    return Promise.resolve(response({}));
  }));
  mount("/profile");
  const input = await screen.findByLabelText("Ссылка на резюме на HH.ru");
  expect(input).toBeDisabled();
  expect(within(input.closest("article") as HTMLElement).getByRole("button", { name: "Сохранить ссылку" })).toBeDisabled();
  expect(calls.some((call) => call.url.includes("/preview") || call.url.includes("/confirm"))).toBe(false);
  finishLoad([]);
  await waitFor(() => expect(input).toBeEnabled());
});

test("initial source load error has an explicit retry and never starts profile mutations", async () => {
  let getCount = 0;
  const calls: Call[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input); calls.push({ url, init });
    if (url.endsWith("/api/resume-sources") && !init?.method) { getCount += 1; return response(getCount === 1 ? { message: "offline" } : [] , getCount !== 1); }
    if (url.endsWith("/api/notifications")) return response([]);
    return response({});
  }));
  mount("/profile");
  expect(await screen.findByRole("alert")).toHaveTextContent("Не удалось загрузить сохранённые резюме.");
  const input = screen.getByLabelText("Ссылка на резюме на HH.ru");
  expect(input).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Повторить" }));
  await waitFor(() => expect(input).toBeEnabled());
  expect(getCount).toBe(2);
  expect(calls.some((call) => call.url.includes("/preview") || call.url.includes("/confirm"))).toBe(false);
});

test.each(sites)("saves an explicitly submitted allowlisted URL for %s", async (adapterId, siteLabel) => {
  const sourceUrl = `https://${adapterId === "hh" ? "hh.ru" : adapterId === "hirehi" ? "hirehi.ru" : "zarplata.ru"}/resume/public-${adapterId}-1`;
  const server = installServer();
  mount("/profile");
  const input = await readyInput(`Ссылка на резюме на ${siteLabel}`);
  fireEvent.change(input, { target: { value: sourceUrl } });
  const card = within(input.closest("article") as HTMLElement);
  expect(card.getByRole("button", { name: "Сохранить ссылку" })).toBeEnabled();
  fireEvent.click(card.getByRole("button", { name: "Сохранить ссылку" }));
  await waitFor(() => expect(input.closest("article")?.querySelector("details.resume-coverage")).toBeInTheDocument());
  const previewCall = server.calls.find((call) => call.url.endsWith("/api/resume-sources/preview"));
  expect(JSON.parse(String(previewCall?.init?.body))).toEqual({ adapter_id: adapterId, resume_url: sourceUrl });
  const confirmCall = server.calls.find((call) => call.url.endsWith("/api/resume-sources/confirm"));
  expect(JSON.parse(String(confirmCall?.init?.body))).toEqual({ adapter_id: adapterId, preview_token: "preview-token-12345678901234567890", consent: true });
  expect(server.getSources().some((item) => (item as { adapter_id?: string }).adapter_id === adapterId)).toBe(true);
  expect(within(input.closest("article") as HTMLElement).getByRole("button", { name: "Обновить данные" })).toBeInTheDocument();
});

test("rapid repeated save clicks create only one in-flight preview and confirm", async () => {
  let finishPreview!: (result: unknown) => void;
  const previewResult = new Promise<unknown>((resolve) => { finishPreview = resolve; });
  const server = installServer({ previewResult }); mount("/profile");
  const input = await readyInput("Ссылка на резюме на HH.ru");
  const card = within(input.closest("article") as HTMLElement);
  fireEvent.change(input, { target: { value: "https://hh.ru/resume/once-1" } });
  fireEvent.click(card.getByRole("button", { name: "Сохранить ссылку" }));
  fireEvent.click(card.getByRole("button", { name: "Проверяем…" }));
  expect(server.calls.filter((call) => call.url.endsWith("/api/resume-sources/preview"))).toHaveLength(1);
  finishPreview({ preview_token: "preview-token-12345678901234567890", preview: { ...preview, source_site: "hh" } });
  await waitFor(() => expect(server.calls.filter((call) => call.url.endsWith("/api/resume-sources/confirm"))).toHaveLength(1));
});

test.each([
  "http://hh.ru/resume/id123",
  "https://user@hh.ru/resume/id123",
  "https://hh.ru:8443/resume/id123",
  "https://hh.ru/resume/id123?print=true",
  "https://hh.ru/resume/id123#section",
  "https://evil.test/resume/id123",
])("rejects an unsafe URL locally without a preview request: %s", async (url) => {
  const server = installServer(); mount("/profile");
  const input = await readyInput("Ссылка на резюме на HH.ru");
  const card = within(input.closest("article") as HTMLElement);
  fireEvent.change(input, { target: { value: url } });
  fireEvent.click(card.getByRole("button", { name: "Сохранить ссылку" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(/прямую ссылку.*HTTPS/u);
  expect(server.calls.some((call) => call.url.endsWith("/preview") || call.url.endsWith("/confirm"))).toBe(false);
});

test.each([
  ["short token", "short", preview],
  ["wrong site", "preview-token-12345678901234567890", { ...preview, source_site: "hirehi" }],
])("does not confirm a preview with %s", async (_caseName, previewToken, previewBody) => {
  const server = installServer({ previewToken, preview: previewBody }); mount("/profile");
  const input = await readyInput("Ссылка на резюме на HH.ru");
  const card = within(input.closest("article") as HTMLElement);
  fireEvent.change(input, { target: { value: "https://hh.ru/resume/public-1" } });
  fireEvent.click(card.getByRole("button", { name: "Сохранить ссылку" }));
  expect(await screen.findByRole("alert")).toBeInTheDocument();
  expect(server.calls.some((call) => call.url.endsWith("/api/resume-sources/confirm"))).toBe(false);
});

test("refreshes only after an explicit click and updates from the server response", async () => {
  const sourceUrl = "https://hh.ru/resume/public-1";
  const server = installServer({ sources: [{ ...saved(), source_url: sourceUrl }], refresh: { ...saved("valid", "fresh-token"), source_url: sourceUrl } });
  mount("/profile");
  const input = await readyInput("Ссылка на резюме на HH.ru");
  expect(input).toHaveValue(sourceUrl);
  expect(server.calls.some((call) => call.url.endsWith("/refresh"))).toBe(false);
  fireEvent.click(screen.getByRole("button", { name: "Обновить данные" }));
  await waitFor(() => expect(server.calls.some((call) => call.url.endsWith("/api/resume-sources/hh/refresh") && call.init?.method === "POST")).toBe(true));
  expect(screen.getByLabelText("Ссылка на резюме на HH.ru")).toHaveValue(sourceUrl);
});

test("an unavailable refresh response keeps the saved URL and reports a warning", async () => {
  const sourceUrl = "https://hh.ru/resume/public-1";
  const server = installServer({ sources: [{ ...saved(), source_url: sourceUrl }], refresh: { ...saved("unavailable", "", preview, undefined, "hh", "ready"), source_url: sourceUrl } });
  mount("/profile"); await readyInput("Ссылка на резюме на HH.ru");
  fireEvent.click(screen.getByRole("button", { name: "Обновить данные" }));
  expect(await screen.findByText((_, element) => element?.textContent === "Не удалось обновить резюме. Сохранённая копия данных остаётся доступной.")).toBeInTheDocument();
  expect(screen.getByLabelText("Ссылка на резюме на HH.ru")).toHaveValue(sourceUrl);
  expect(server.calls.some((call) => call.url.endsWith("/api/resume-sources/hh/refresh"))).toBe(true);
});

test("editing is opt-in and cancelling restores the saved URL without a request", async () => {
  const sourceUrl = "https://hh.ru/resume/original-1";
  const server = installServer({ sources: [{ ...saved(), source_url: sourceUrl }] }); mount("/profile");
  const input = await readyInput("Ссылка на резюме на HH.ru");
  expect(input).toHaveAttribute("readonly");
  fireEvent.click(screen.getByRole("button", { name: "Редактировать ссылку на HH.ru" }));
  expect(input).not.toHaveAttribute("readonly");
  fireEvent.change(input, { target: { value: "https://hh.ru/resume/draft-2" } });
  fireEvent.click(screen.getByRole("button", { name: "Отменить редактирование ссылки на HH.ru" }));
  expect(input).toHaveValue(sourceUrl);
  expect(input).toHaveAttribute("readonly");
  expect(server.calls.some((call) => call.url.endsWith("/preview") || call.url.endsWith("/confirm"))).toBe(false);
});

test("a failed replacement confirm preserves the prior saved record and coverage", async () => {
  const originalUrl = "https://hh.ru/resume/original-1";
  const server = installServer({ sources: [{ ...saved("valid", "old-token", { sections: [{ key: "experience", status: "present" }] }), source_url: originalUrl }], confirmOk: false }); mount("/profile");
  const input = await readyInput("Ссылка на резюме на HH.ru");
  fireEvent.click(screen.getByRole("button", { name: "Редактировать ссылку на HH.ru" }));
  fireEvent.change(input, { target: { value: "https://hh.ru/resume/replacement-2" } });
  fireEvent.click(screen.getByRole("button", { name: "Сохранить изменения" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Ошибка запроса");
  expect(input).toHaveValue("https://hh.ru/resume/replacement-2");
  expect(within(input.closest("article") as HTMLElement).getByRole("img", { name: "Опыт работы: Найдено" })).toBeInTheDocument();
  expect(server.getSources()).toHaveLength(1);
  expect((server.getSources()[0] as { source_url?: string }).source_url).toBe(originalUrl);
});

test("unsupported required preview questions are not fabricated or confirmed", async () => {
  const server = installServer({ preview: { ...preview, questions: [{ id: "unknown-required", question: "Неизвестный обязательный вопрос", required: true }] } }); mount("/profile");
  const input = await readyInput("Ссылка на резюме на HH.ru");
  const card = within(input.closest("article") as HTMLElement);
  fireEvent.change(input, { target: { value: "https://hh.ru/resume/public-1" } });
  fireEvent.click(card.getByRole("button", { name: "Сохранить ссылку" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("неподдерживаемый ответ");
  expect(server.calls.some((call) => call.url.endsWith("/api/resume-sources/confirm"))).toBe(false);
});

test("delete confirmation cancel makes no request and keeps the saved source", async () => {
  const sourceUrl = "https://hh.ru/resume/original-1";
  const server = installServer({ sources: [{ ...saved(), source_url: sourceUrl }] }); mount("/profile");
  await readyInput("Ссылка на резюме на HH.ru");
  fireEvent.click(screen.getByRole("button", { name: "Удалить резюме для HH.ru" }));
  expect(await screen.findByRole("alertdialog")).toHaveTextContent(`Вы точно хотите удалить резюме ${sourceUrl} для сайта HH.ru?`);
  fireEvent.click(screen.getByRole("button", { name: "Нет" }));
  expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Обновить данные" })).toBeInTheDocument();
  expect(server.calls.some((call) => call.url.endsWith("/api/resume-sources/hh") && call.init?.method === "DELETE")).toBe(false);
});

test("delete uses the saved URL during editing and removes only that site's record", async () => {
  const sourceUrl = "https://hh.ru/resume/original-1";
  const server = installServer({ sources: [{ ...saved(), source_url: sourceUrl }, saved("valid", "other-token", preview, undefined, "hirehi")] }); mount("/profile");
  const input = await readyInput("Ссылка на резюме на HH.ru");
  fireEvent.click(screen.getByRole("button", { name: "Редактировать ссылку на HH.ru" }));
  fireEvent.change(input, { target: { value: "https://hh.ru/resume/draft-2" } });
  fireEvent.click(screen.getByRole("button", { name: "Удалить резюме для HH.ru" }));
  expect(await screen.findByRole("alertdialog")).toHaveTextContent(`Вы точно хотите удалить резюме ${sourceUrl} для сайта HH.ru?`);
  fireEvent.click(screen.getByRole("button", { name: "Да" }));
  await waitFor(() => expect(server.calls.some((call) => call.url.endsWith("/api/resume-sources/hh") && call.init?.method === "DELETE")).toBe(true));
  expect(server.getSources().map((item) => (item as { adapter_id?: string }).adapter_id)).toEqual(["hirehi"]);
  expect(await screen.findByRole("button", { name: "Сохранить ссылку" })).toBeInTheDocument();
  expect(screen.getByLabelText("Ссылка на резюме на HH.ru")).toHaveValue("");
});

test("delete failure keeps the confirmation open and saved source visible", async () => {
  const sourceUrl = "https://hh.ru/resume/original-1";
  const server = installServer({ sources: [{ ...saved(), source_url: sourceUrl }], deleteOk: false }); mount("/profile");
  await readyInput("Ссылка на резюме на HH.ru");
  fireEvent.click(screen.getByRole("button", { name: "Удалить резюме для HH.ru" }));
  fireEvent.click(await screen.findByRole("button", { name: "Да" }));
  expect(await screen.findByRole("alertdialog")).toHaveTextContent("Ошибка запроса");
  expect(screen.getByLabelText("Ссылка на резюме на HH.ru")).toHaveValue(sourceUrl);
  expect(screen.getByRole("button", { name: "Нет" })).toBeInTheDocument();
  expect(server.getSources()).toHaveLength(1);
});

test("required gender is asked only when preview requires it and is sent on explicit confirm", async () => {
  const server = installServer({ sources: [{ ...saved(), source_url: "https://hh.ru/resume/original-1" }], preview: genderPreview }); mount("/profile");
  const input = await readyInput("Ссылка на резюме на HH.ru");
  fireEvent.click(screen.getByRole("button", { name: "Редактировать ссылку на HH.ru" }));
  fireEvent.change(input, { target: { value: "https://hh.ru/resume/replacement-2" } });
  fireEvent.click(screen.getByRole("button", { name: "Сохранить изменения" }));
  expect(await screen.findByRole("dialog")).toHaveTextContent("Род для сопроводительных писем");
  expect(server.calls.some((call) => call.url.endsWith("/api/resume-sources/confirm"))).toBe(false);
  fireEvent.change(screen.getByRole("combobox", { name: "Род" }), { target: { value: "female" } });
  fireEvent.click(screen.getByRole("button", { name: "Сохранить" }));
  await waitFor(() => expect(server.calls.some((call) => call.url.endsWith("/api/resume-sources/confirm"))).toBe(true));
  const body = JSON.parse(String(server.calls.find((call) => call.url.endsWith("/api/resume-sources/confirm"))?.init?.body));
  expect(body).toMatchObject({ adapter_id: "hh", grammatical_gender: "female", consent: true });
});

test("cancelling the required gender prompt never confirms or replaces the saved source", async () => {
  const sourceUrl = "https://hh.ru/resume/original-1";
  const server = installServer({ sources: [{ ...saved(), source_url: sourceUrl }], preview: genderPreview }); mount("/profile");
  const input = await readyInput("Ссылка на резюме на HH.ru");
  fireEvent.click(screen.getByRole("button", { name: "Редактировать ссылку на HH.ru" }));
  fireEvent.change(input, { target: { value: "https://hh.ru/resume/replacement-2" } });
  fireEvent.click(screen.getByRole("button", { name: "Сохранить изменения" }));
  fireEvent.click(await screen.findByRole("button", { name: "Отмена" }));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(server.calls.some((call) => call.url.endsWith("/api/resume-sources/confirm"))).toBe(false);
  expect(screen.getByLabelText("Ссылка на резюме на HH.ru")).toHaveValue("https://hh.ru/resume/replacement-2");
  expect(screen.getByText("Полученные данные")).toBeInTheDocument();
  expect(server.getSources()).toHaveLength(1);
});

test("a saved source on one site does not make another site ready", async () => {
  const server = installServer({ sources: [saved()] });
  mount("/session");
  const site = await screen.findByRole("combobox", { name: "Сайт" });
  fireEvent.click(site);
  const siteOption = await screen.findByRole("option", { name: "HireHi" });
  fireEvent.pointerDown(siteOption, { pointerType: "mouse", button: 0 });
  fireEvent.mouseUp(siteOption);
  fireEvent.click(siteOption, { detail: 1 });
  const launch = await screen.findByRole("button", { name: "Создать и запустить" });
  expect(launch).toBeEnabled();
  fireEvent.click(launch);
  expect(await screen.findByText('Для сайта HireHi не загружено резюме, проверьте раздел "Профиль"')).toBeInTheDocument();
  expect(server.calls.some((call) => call.url.endsWith("/api/sessions") && call.init?.method === "POST")).toBe(false);
  expect(document.querySelector(".session-resume-requirement")).not.toBeInTheDocument();
  expect([...document.querySelectorAll(".notice")].some((notice) => notice.textContent?.includes("Добавьте ссылку на резюме сайта HireHi во вкладке Профиль."))).toBe(true);
  expect(document.querySelectorAll('a[href="/profile"]').length).toBeGreaterThan(1);
});

test.each(sites.flatMap(([adapterId, label]) => [
  [adapterId, label, "missing" as const],
  [adapterId, label, "corrupt" as const],
]))("keeps launch blocked for %s when its saved copy is %s", async (adapterId, label, dataStatus) => {
  const server = installServer({ sources: [saved("valid", "saved-token", {}, undefined, adapterId, dataStatus)] });
  mount("/session");
  await chooseOption("Сайт", label);
  const launch = screen.getByRole("button", { name: "Создать и запустить" });
  expect(launch).toBeEnabled();
  fireEvent.click(launch);
  expect(await screen.findAllByText(`Для сайта ${label} не удалось извлечь необходимые данные из резюме, проверьте раздел "Профиль"`)).not.toHaveLength(0);
  expect(server.calls.some((call) => call.url.endsWith("/api/sessions") && call.init?.method === "POST")).toBe(false);
  expect([...document.querySelectorAll(".notice")].some((notice) => notice.textContent?.includes("Обновите данные резюме во вкладке Профиль."))).toBe(true);
  expect(document.querySelector(".session-resume-requirement")).not.toBeInTheDocument();
  expect(document.querySelectorAll('a[href="/profile"]').length).toBeGreaterThan(1);
});

test("HireHi launches from a ready local copy without a preview token or availability refresh", async () => {
  const server = installServer({ sources: [saved("unavailable", "", preview, undefined, "hirehi")] });
  mount("/session");
  await chooseOption("Сайт", "HireHi");
  const launch = await screen.findByRole("button", { name: "Создать и запустить" });
  await waitFor(() => expect(launch).toBeEnabled());
  fireEvent.click(launch);
  await waitFor(() => expect(server.calls.some((call) => call.url.endsWith("/api/sessions") && call.init?.method === "POST")).toBe(true));
  expect(server.calls.some((call) => call.url.endsWith("/api/resume-sources/hirehi/refresh"))).toBe(false);
  expect(server.calls.filter((call) => call.url.endsWith("/api/resume-sources") && !call.init?.method)).toHaveLength(1);
  expect(JSON.parse(String(server.calls.find((call) => call.url.endsWith("/api/sessions") && call.init?.method === "POST")?.init?.body))).not.toHaveProperty("resume_preview_token");
});

test("launch works with a durable gender preference and sends no gender answer", async () => {
  const server = installServer({ sources: [saved("valid", "saved-token", genderPreview, "female")] });
  mount("/session");
  expect(screen.queryByText(/род использовать/)).not.toBeInTheDocument();
  const launch = await screen.findByRole("button", { name: "Создать и запустить" });
  await waitFor(() => expect(launch).toBeEnabled());
  fireEvent.click(launch);
  await waitFor(() => expect(server.calls.some((call) => call.url.endsWith("/api/sessions") && call.init?.method === "POST")).toBe(true));
  const createCall = server.calls.find((call) => call.url.endsWith("/api/sessions") && call.init?.method === "POST");
  expect(JSON.parse(String(createCall?.init?.body))).not.toHaveProperty("resume_answers");
  expect(JSON.parse(String(createCall?.init?.body))).not.toHaveProperty("grammatical_gender");
  expect(server.calls.filter((call) => call.url.endsWith("/api/sessions") && call.init?.method === "POST")).toHaveLength(1);
  expect(server.calls.some((call) => call.url.endsWith("/api/resume-sources/hh/refresh"))).toBe(false);
});

test("does not ask to reconfirm an unavailable saved source", async () => {
  installServer({ sources: [saved("unavailable")] });
  mount("/profile");
  const card = screen.getByText("HH.ru").closest("article") as HTMLElement;
  await waitFor(() => expect(within(card).getByLabelText("Ссылка на резюме на HH.ru")).toBeEnabled());
  expect(within(card).queryByRole("checkbox")).not.toBeInTheDocument();
  expect(within(card).getByRole("button", { name: "Обновить данные" })).toBeInTheDocument();
});

test("does not poll the saved source query", async () => {
  const server = installServer({ sources: [saved()] });
  mount("/profile");
  await screen.findByRole("heading", { name: "HH.ru" });
  const initialSourceGets = server.calls.filter((call) => call.url.endsWith("/api/resume-sources") && !call.init?.method).length;
  await new Promise((resolve) => setTimeout(resolve, 2600));
  expect(server.calls.filter((call) => call.url.endsWith("/api/resume-sources") && !call.init?.method)).toHaveLength(initialSourceGets);
});
