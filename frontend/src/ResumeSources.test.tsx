import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import App from "./App";
import { previewSections, resumeContactLabel } from "./resumeSources";

const genderQuestion = { id: "grammatical_gender", question: "Какой род использовать в сопроводительных письмах?", options: ["male", "female"], required: true };
const preview = { source_site: "hh", target_title: "Product Manager", questions: [] };
const genderPreview = { ...preview, questions: [genderQuestion] };
const saved = (status: "valid" | "changed" | "unavailable" = "valid", token = "saved-token", sourcePreview: unknown = preview, grammaticalGender?: "male" | "female", adapterId = "hh", resumeDataStatus: "ready" | "missing" | "corrupt" = "ready") => ({ adapter_id: adapterId, status, checked_at: "2026-09-14T10:00:00Z", uses_saved_data: adapterId !== "hirehi", resume_data_status: adapterId === "hirehi" ? null : resumeDataStatus, resume_data_saved_at: adapterId === "hirehi" ? null : "2026-09-14T10:00:00Z", resume_data_error_message: null, preview_token: status === "unavailable" ? undefined : token, preview: sourcePreview, changed: status === "changed", ...(grammaticalGender ? { grammatical_gender: grammaticalGender } : {}) });
type Call = { url: string; init?: RequestInit };

function mount(path: string, client = new QueryClient({ defaultOptions: { queries: { retry: false } } })) {
  return { ...render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[path]}><App /></MemoryRouter></QueryClientProvider>), client };
}
function response(body: unknown, ok = true) { return { ok, status: ok ? 200 : 422, json: async () => body }; }
async function chooseOption(label: string, option: string) {
  fireEvent.click(await screen.findByRole("combobox", { name: label }));
  const item = await screen.findByRole("option", { name: option });
  fireEvent.pointerDown(item, { pointerType: "mouse", button: 0 });
  fireEvent.mouseUp(item);
  fireEvent.click(item, { detail: 1 });
}
function installServer(options: { sources?: unknown[]; confirm?: unknown; refresh?: unknown; refreshOk?: boolean; refreshResult?: Promise<unknown>; create?: unknown; createOk?: boolean; preview?: unknown } = {}) {
  const calls: Call[] = [];
  let sources = options.sources ?? [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input); calls.push({ url, init });
    if (url.endsWith("/api/resume-sources") && !init?.method) return response(sources);
    if (url.endsWith("/api/resume-sources/preview")) return response({ preview_token: "preview-token", preview: options.preview ?? preview });
    if (url.endsWith("/api/resume-sources/confirm")) { const body = JSON.parse(String(init?.body ?? "{}")); const confirmed = saved("valid", "saved-token", options.preview ?? preview, body.grammatical_gender, body.adapter_id); sources = [confirmed]; return response(options.confirm ?? confirmed); }
    const refreshAdapter = url.match(/\/api\/resume-sources\/(hh|zarplata)\/refresh$/u)?.[1];
    if (refreshAdapter) return options.refreshResult ? options.refreshResult.then((result) => response(result)) : response(options.refresh ?? saved("valid", "fresh-token", preview, undefined, refreshAdapter), options.refreshOk !== false);
    if (url.endsWith("/api/resume-sources/hh") && init?.method === "PATCH") { const body = JSON.parse(String(init.body ?? "{}")); const current = sources[0] as Record<string, unknown> | undefined; sources = [saved("valid", "saved-token", current?.preview ?? preview, body.grammatical_gender)]; return response(sources[0]); }
    if (url.endsWith("/api/resume-sources/hh") && init?.method === "DELETE") { sources = []; return response({}); }
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
});

test("loads saved sources from server after remount and purges legacy storage", async () => {
  sessionStorage.setItem("job-orchestrator.resume-sources", JSON.stringify({ hh: { previewToken: "secret-token", resume_url: "https://hh.ru/resume/secret" } }));
  localStorage.setItem("job-orchestrator.resume-sources", JSON.stringify({ hh: { previewToken: "legacy-token" } }));
  sessionStorage.setItem("keep-session-state", "keep");
  localStorage.setItem("keep-local-state", "keep");
  const server = installServer({ sources: [saved()] });
  const first = mount("/profile");
  expect(await screen.findByText("Product Manager")).toBeInTheDocument();
  expect(screen.getByText("Актуально")).toBeInTheDocument();
  expect(sessionStorage.getItem("job-orchestrator.resume-sources")).toBeNull();
  expect(localStorage.getItem("job-orchestrator.resume-sources")).toBeNull();
  first.unmount();
  mount("/profile", new QueryClient({ defaultOptions: { queries: { retry: false } } }));
  await waitFor(() => expect(server.calls.filter((call) => call.url.endsWith("/api/resume-sources") && !call.init?.method)).toHaveLength(2));
  expect(sessionStorage.getItem("keep-session-state")).toBe("keep");
  expect(localStorage.getItem("keep-local-state")).toBe("keep");
});

test("renders the full public source_url returned by the server", async () => {
  const sourceUrl = "https://hh.ru/resume/public-resume-123";
  installServer({ sources: [{ adapter_id: "hh", status: "valid", checked_at: "2026-09-14T10:00:00Z", source_url: sourceUrl, preview: preview }] });
  mount("/profile");
  const link = await screen.findByRole("link", { name: sourceUrl });
  expect(link).toHaveAttribute("href", sourceUrl);
  expect(link.textContent).toBe(sourceUrl);
});

test("confirms only after server POST and never stores URL/token", async () => {
  const server = installServer(); mount("/profile");
  fireEvent.change(screen.getByLabelText("Ссылка на резюме на HH.ru"), { target: { value: "https://hh.ru/resume/abc123" } });
  fireEvent.click(screen.getAllByRole("button", { name: "Проверить ссылку" })[0]);
  await screen.findByText("Product Manager");
  fireEvent.click(screen.getByRole("checkbox", { name: /Это моё резюме/ }));
  expect(screen.getByText(/Разрешаю сохранить ссылку/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Подтвердить резюме" }));
  await waitFor(() => expect(screen.getByText("Актуально")).toBeInTheDocument());
  const confirmCall = server.calls.find((call) => call.url.endsWith("/api/resume-sources/confirm"));
  expect(JSON.parse(String(confirmCall?.init?.body))).toEqual({ adapter_id: "hh", preview_token: "preview-token", consent: true });
  expect(sessionStorage.length).toBe(0); expect(localStorage.length).toBe(0);
});

test("changed and unavailable sources remain confirmed and removable explicitly", async () => {
  installServer({ sources: [saved("changed")] }); mount("/profile");
  expect(await screen.findByText("Обновлено")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Заменить источник" })).toBeInTheDocument();
  expect(screen.getByText("Ссылка сохранена")).toBeInTheDocument();
  cleanup(); vi.unstubAllGlobals();
  installServer({ sources: [saved("unavailable")] }); mount("/profile");
  expect(await screen.findByText("Недоступно")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Заменить источник" })).toBeInTheDocument();
  expect(screen.getByText("Ссылка сохранена")).toBeInTheDocument();
});

test("delete uses server DELETE", async () => {
  const server = installServer({ sources: [saved()] }); mount("/profile");
  await screen.findByText("Актуально"); fireEvent.click(screen.getByRole("button", { name: "Удалить источник" }));
  fireEvent.click(screen.getByRole("button", { name: /^Удалить$/ }));
  await waitFor(() => expect(server.calls.some((call) => call.url.endsWith("/api/resume-sources/hh") && call.init?.method === "DELETE")).toBe(true));
  await waitFor(() => expect(screen.queryByText("Актуально")).not.toBeInTheDocument());
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
  expect(screen.getByText("Резюме для HH.ru")).toBeInTheDocument();
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
  await waitFor(() => expect(screen.getByText("Ссылка сохранена")).toBeInTheDocument());
  const stop = await screen.findByRole("button", { name: "Остановить" }); fireEvent.click(stop);
  await waitFor(() => expect(calls.some((call) => call.url.endsWith("/api/sessions/7/stop"))).toBe(true));
  fireEvent.click(screen.getByRole("link", { name: "Профиль" }));
  expect(await screen.findByText("Ссылка сохранена")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("link", { name: "Сессия" }));
  expect(await screen.findByText("Ссылка сохранена")).toBeInTheDocument();
  expect(calls.some((call) => call.init?.method === "DELETE")).toBe(false);
});

test("launch error leaves the saved source visible", async () => {
  const server = installServer({ sources: [saved("unavailable")], createOk: false }); mount("/session");
  const launch = await screen.findByRole("button", { name: "Создать и запустить" }); await waitFor(() => expect(launch).toBeEnabled()); fireEvent.click(launch);
  expect(await screen.findByText("Сессия использует сохранённые данные резюме.")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Открыть источник" })).toBeInTheDocument();
  await waitFor(() => expect(server.calls.filter((call) => call.url.endsWith("/api/sessions") && call.init?.method === "POST")).toHaveLength(1));
  expect(screen.getByText("Резюме для HH.ru")).toBeInTheDocument();
});

test("persists profile gender, restores it after refetch, and keeps it out of Session", async () => {
  const server = installServer({ sources: [], preview: genderPreview }); const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const profile = mount("/profile", client);
  fireEvent.change(screen.getByLabelText("Ссылка на резюме на HH.ru"), { target: { value: "https://hh.ru/resume/abc123" } }); fireEvent.click(screen.getAllByRole("button", { name: "Проверить ссылку" })[0]); await screen.findByText("Product Manager");
  fireEvent.change(screen.getByRole("combobox", { name: genderQuestion.question }), { target: { value: "female" } }); fireEvent.click(screen.getByRole("checkbox", { name: /Это моё резюме/ })); fireEvent.click(screen.getByRole("button", { name: "Подтвердить резюме" })); await screen.findByText("Актуально"); profile.unmount();
  const confirmCall = server.calls.find((call) => call.url.endsWith("/api/resume-sources/confirm"));
  expect(JSON.parse(String(confirmCall?.init?.body))).toMatchObject({ grammatical_gender: "female" });
  mount("/profile", client); expect(await screen.findByRole("combobox", { name: genderQuestion.question })).toHaveValue("female");
  fireEvent.change(screen.getByRole("combobox", { name: genderQuestion.question }), { target: { value: "male" } });
  await waitFor(() => expect(server.calls.some((call) => call.url.endsWith("/api/resume-sources/hh") && call.init?.method === "PATCH")).toBe(true));
  await client.invalidateQueries({ queryKey: ["resume-sources"] });
  await waitFor(() => expect(screen.getByRole("combobox", { name: genderQuestion.question })).toHaveValue("male"));
  cleanup();
  mount("/session", client);
  expect(screen.queryByRole("combobox", { name: genderQuestion.question })).not.toBeInTheDocument();
  expect(screen.queryByText(/род использовать/)).not.toBeInTheDocument();
  expect(sessionStorage.length).toBe(0); expect(localStorage.getItem("job-orchestrator.resume-sources")).toBeNull();
  expect(server.calls.some((call) => call.url.endsWith("/api/resume-sources/hh/refresh"))).toBe(false);
});

test("keeps the saved source visible when auto-start creation fails", async () => {
  const server = installServer({ sources: [saved()], createOk: false });
  mount("/session");
  const launch = await screen.findByRole("button", { name: "Создать и запустить" });
  await waitFor(() => expect(launch).toBeEnabled());
  fireEvent.click(launch);
  await waitFor(() => expect(server.calls.some((call) => call.url.endsWith("/api/sessions") && call.init?.method === "POST")).toBe(true));
  expect(await screen.findByRole("alert")).toHaveTextContent("Ошибка запроса");
  await waitFor(() => expect(screen.getByText("Резюме для HH.ru")).toBeInTheDocument());
  expect(screen.queryByText("Добавьте ссылку в профиле")).not.toBeInTheDocument();
});

test.each([
  ["hh", "HH.ru", "https://hh.ru/resume/hh-123"],
  ["hirehi", "HireHi", "https://hirehi.ru/resume/hirehi-123"],
  ["zarplata", "Zarplata.ru", "https://zarplata.ru/resume/zarplata-123"],
] as const)("previews and confirms a source on %s", async (adapterId, siteLabel, resumeUrl) => {
  const calls: Call[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input); calls.push({ url, init });
    if (url.endsWith("/api/resume-sources") && !init?.method) return response([]);
    if (url.endsWith("/api/resume-sources/preview")) return response({ preview_token: "preview-token-123456789012345", preview: { source_site: adapterId, target_title: `${siteLabel} role`, questions: [] } });
    if (url.endsWith("/api/resume-sources/confirm")) return response({ ...saved("valid", "saved-token-123456789012345"), adapter_id: adapterId, preview: { source_site: adapterId, target_title: `${siteLabel} role`, questions: [] } });
    return response({});
  }));
  mount("/profile");
  const input = screen.getByLabelText(`Ссылка на резюме на ${siteLabel}`);
  const card = input.closest("article") as HTMLElement;
  fireEvent.change(input, { target: { value: resumeUrl } });
  fireEvent.click(within(card).getByRole("button", { name: "Проверить ссылку" }));
  expect(await within(card).findByText(`${siteLabel} role`)).toBeInTheDocument();
  fireEvent.click(within(card).getByRole("checkbox", { name: /Это моё резюме/ }));
  fireEvent.click(within(card).getByRole("button", { name: "Подтвердить резюме" }));
  await waitFor(() => expect(within(card).getByText("Актуально")).toBeInTheDocument());
  if (adapterId === "hh" || adapterId === "zarplata") expect(within(card).getByText("Данные сохранены на этом компьютере")).toBeInTheDocument();
  const confirmCall = calls.find((call) => call.url.endsWith("/api/resume-sources/confirm"));
  expect(JSON.parse(String(confirmCall?.init?.body))).toEqual({ adapter_id: adapterId, preview_token: "preview-token-123456789012345", consent: true });
});

test.each([["hh", "HH.ru"], ["zarplata", "Zarplata.ru"]] as const)("manual refresh on %s updates saved data while retaining a good copy after unavailable", async (adapterId, label) => {
  const source = saved("valid", "saved-token", preview, undefined, adapterId);
  const failedRefresh = { ...saved("unavailable", "fresh-token", preview, undefined, adapterId), resume_data_status: "ready", resume_data_error_message: "Ссылка временно недоступна" };
  const server = installServer({ sources: [source], refresh: failedRefresh });
  mount("/profile");
  const card = screen.getByText(label).closest("article") as HTMLElement;
  expect(await within(card).findByText("Данные сохранены на этом компьютере")).toBeInTheDocument();
  fireEvent.click(within(card).getByRole("button", { name: "Обновить данные" }));
  await waitFor(() => expect(within(card).getByText("Не удалось обновить источник")).toBeInTheDocument());
  expect(within(card).getByText("Данные сохранены на этом компьютере")).toBeInTheDocument();
  const refreshCalls = server.calls.filter((call) => call.url.endsWith(`/api/resume-sources/${adapterId}/refresh`) && call.init?.method === "POST");
  expect(refreshCalls).toHaveLength(1);
  cleanup();
  mount("/session");
  await chooseOption("Сайт", label);
  await waitFor(() => expect(screen.getByRole("button", { name: "Создать и запустить" })).toBeEnabled());
  expect(screen.getByText("Сессия использует сохранённые данные резюме.")).toBeInTheDocument();
});

test.each(["missing", "corrupt"] as const)("blocks launch when HH saved data is %s and directs to Profile", async (resumeDataStatus) => {
  installServer({ sources: [saved("valid", "saved-token", {}, undefined, "hh", resumeDataStatus)] });
  mount("/session");
  await chooseOption("Сайт", "HH.ru");
  expect((await screen.findAllByText(/Обновите данные резюме во вкладке «Профиль»/)).length).toBeGreaterThan(0);
  expect(screen.getByRole("button", { name: "Создать и запустить" })).toBeDisabled();
  fireEvent.click(screen.getByRole("link", { name: "Профиль" }));
  expect(await screen.findByRole("button", { name: "Обновить данные" })).toBeEnabled();
});

test("disables refresh while pending and keeps the ready copy after a 200 unavailable result", async () => {
  let release!: (value: unknown) => void;
  const pendingResult = new Promise<unknown>((resolve) => { release = resolve; });
  const server = installServer({ sources: [saved()], refreshResult: pendingResult });
  const cardPage = mount("/profile");
  const card = screen.getByText("HH.ru").closest("article") as HTMLElement;
  expect(await within(card).findByText("Данные сохранены на этом компьютере")).toBeInTheDocument();
  const refresh = await within(card).findByRole("button", { name: "Обновить данные" });
  expect(refresh).toBeEnabled();
  fireEvent.click(refresh);
  await waitFor(() => expect(server.calls.filter((call) => call.url.endsWith("/api/resume-sources/hh/refresh"))).toHaveLength(1));
  expect(await screen.findByRole("button", { name: "Обновляем…" })).toBeDisabled();
  release({ ...saved("unavailable"), resume_data_status: "ready", resume_data_error_message: "Ссылка временно недоступна" });
  expect(await screen.findByRole("alert")).toHaveTextContent("Ссылка временно недоступна");
  expect(screen.getByText("Данные сохранены на этом компьютере")).toBeInTheDocument();
  expect(server.calls.filter((call) => call.url.endsWith("/api/resume-sources/hh/refresh"))).toHaveLength(1);
  cardPage.unmount();
});

test("an HTTP refresh error leaves the last ready copy usable", async () => {
  const server = installServer({ sources: [saved()], refreshOk: false });
  mount("/profile");
  const card = screen.getByText("HH.ru").closest("article") as HTMLElement;
  expect(await within(card).findByText("Данные сохранены на этом компьютере")).toBeInTheDocument();
  fireEvent.click(within(card).getByRole("button", { name: "Обновить данные" }));
  expect(await within(card).findByRole("alert")).toHaveTextContent("Ошибка запроса");
  expect(within(card).getByText("Данные сохранены на этом компьютере")).toBeInTheDocument();
  expect(server.calls.filter((call) => call.url.endsWith("/api/resume-sources/hh/refresh"))).toHaveLength(1);
});

test("a legacy HH record without saved-data metadata is treated as missing", async () => {
  installServer({ sources: [{ adapter_id: "hh", status: "valid", preview: {} }] });
  mount("/session");
  await chooseOption("Сайт", "HH.ru");
  expect(screen.getByRole("button", { name: "Создать и запустить" })).toBeDisabled();
  fireEvent.click(screen.getByRole("link", { name: "Профиль" }));
  expect(await screen.findByRole("button", { name: "Обновить данные" })).toBeEnabled();
});

test("does not offer manual saved-data refresh for HireHi", async () => {
  installServer({ sources: [saved("valid", "saved-token", preview, undefined, "hirehi")] });
  mount("/profile");
  expect(await screen.findByText("HireHi")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Обновить данные" })).not.toBeInTheDocument();
});

test("rejects a preview returned for a different site", async () => {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/api/resume-sources") && !init?.method) return response([]);
    if (url.endsWith("/api/resume-sources/preview")) return response({ preview_token: "preview-token-123456789012345", preview: { source_site: "hirehi", target_title: "Wrong site" } });
    return response({});
  }));
  mount("/profile");
  const input = screen.getByLabelText("Ссылка на резюме на HH.ru");
  const card = input.closest("article") as HTMLElement;
  fireEvent.change(input, { target: { value: "https://hh.ru/resume/hh-123" } });
  fireEvent.click(screen.getAllByRole("button", { name: "Проверить ссылку" })[0]);
  expect(await within(card).findByRole("alert")).toHaveTextContent("не совпал с выбранной площадкой");
  expect(within(card).queryByRole("button", { name: "Подтвердить резюме" })).not.toBeInTheDocument();
});

test("surfaces a preview backend error", async () => {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/api/resume-sources") && !init?.method) return response([]);
    if (url.endsWith("/api/resume-sources/preview")) return response({ detail: "Ссылка не читается" }, false);
    return response({});
  }));
  mount("/profile");
  const input = screen.getByLabelText("Ссылка на резюме на HH.ru");
  const card = input.closest("article") as HTMLElement;
  fireEvent.change(input, { target: { value: "https://hh.ru/resume/hh-123" } });
  fireEvent.click(screen.getAllByRole("button", { name: "Проверить ссылку" })[0]);
  expect(await within(card).findByRole("alert")).toHaveTextContent("Ссылка не читается");
});

test("does not expose bearer URL or source id in the DOM or browser storage", async () => {
  const bearerUrl = "https://hh.ru/resume/private-bearer-123";
  const sourceId = "private-source-id-456";
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/api/resume-sources") && !init?.method) return response([]);
    if (url.endsWith("/api/resume-sources/preview")) return response({ preview_token: "preview-token-123456789012345", preview: { source_site: "hh", target_title: "Product Manager", source_resume_id: sourceId, source_edit_url: bearerUrl, source_url: bearerUrl } });
    return response({});
  }));
  mount("/profile");
  const input = screen.getByLabelText("Ссылка на резюме на HH.ru");
  fireEvent.change(input, { target: { value: bearerUrl } });
  fireEvent.click(screen.getAllByRole("button", { name: "Проверить ссылку" })[0]);
  await screen.findByText("Product Manager");
  expect(document.body.textContent).not.toContain(bearerUrl);
  expect(document.body.textContent).not.toContain(sourceId);
  expect(sessionStorage.length).toBe(0);
  expect(localStorage.length).toBe(0);
});

test("a saved source on one site does not make another site ready", async () => {
  installServer({ sources: [saved()] });
  mount("/session");
  const site = await screen.findByRole("combobox", { name: "Сайт" });
  fireEvent.click(site);
  const siteOption = await screen.findByRole("option", { name: "HireHi" });
  fireEvent.pointerDown(siteOption, { pointerType: "mouse", button: 0 });
  fireEvent.mouseUp(siteOption);
  fireEvent.click(siteOption, { detail: 1 });
  const launch = await screen.findByRole("button", { name: "Создать и запустить" });
  expect(launch).toBeDisabled();
  expect(screen.getByText("Резюме для HireHi")).toBeInTheDocument();
  expect(screen.getByText(/Нужно добавить и подтвердить ссылку/)).toBeInTheDocument();
});

test("launch works with a durable gender preference and sends no gender answer", async () => {
  const server = installServer({ sources: [saved("valid", "saved-token", genderPreview, "female")] });
  mount("/session");
  expect(screen.queryByRole("combobox", { name: genderQuestion.question })).not.toBeInTheDocument();
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
  expect(await within(card).findByText("Недоступно")).toBeInTheDocument();
  expect(within(card).queryByRole("checkbox", { name: /Это моё резюме/ })).not.toBeInTheDocument();
  expect(within(card).queryByRole("button", { name: "Подтвердить резюме" })).not.toBeInTheDocument();
});

test("does not poll the saved source query", async () => {
  const server = installServer({ sources: [saved()] });
  mount("/profile");
  await screen.findByText("Актуально");
  const initialSourceGets = server.calls.filter((call) => call.url.endsWith("/api/resume-sources") && !call.init?.method).length;
  await new Promise((resolve) => setTimeout(resolve, 2600));
  expect(server.calls.filter((call) => call.url.endsWith("/api/resume-sources") && !call.init?.method)).toHaveLength(initialSourceGets);
});
