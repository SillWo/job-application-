import { api } from "./api";

afterEach(() => { vi.unstubAllGlobals(); });

test("merges JSON content type with custom session idempotency header", async () => {
  const headers = { "Idempotency-Key": "launch-key" };
  const options: RequestInit = { method: "POST", body: JSON.stringify({ auto_start: true }), headers };
  const fetchMock = vi.fn(async () => ({ ok: true, status: 200, text: async () => "{}" }));
  vi.stubGlobal("fetch", fetchMock);

  await api("/sessions", options);

  const [, request] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
  const sentHeaders = new Headers(request.headers);
  expect(request.body).toBe('{"auto_start":true}');
  expect(sentHeaders.get("Content-Type")).toBe("application/json");
  expect(sentHeaders.get("Idempotency-Key")).toBe("launch-key");
  expect(headers).toEqual({ "Idempotency-Key": "launch-key" });
});

test("preserves FormData custom headers without setting Content-Type", async () => {
  const form = new FormData();
  form.set("resume", "contents");
  const fetchMock = vi.fn(async () => ({ ok: true, status: 200, text: async () => "{}" }));
  vi.stubGlobal("fetch", fetchMock);

  await api("/upload", { method: "POST", body: form, headers: { "X-Upload-Token": "token" } });

  const [, request] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
  const sentHeaders = new Headers(request.headers);
  expect(request.body).toBe(form);
  expect(sentHeaders.get("Content-Type")).toBeNull();
  expect(sentHeaders.get("X-Upload-Token")).toBe("token");
});

test("formats FastAPI validation detail arrays without exposing object stringification", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => ({
    ok: false,
    status: 422,
    statusText: "Unprocessable Entity",
    json: async () => ({ detail: [{ msg: "Field required" }, { msg: 42 }, { msg: "Invalid value" }] }),
  })));

  await expect(api("/sessions", { method: "POST", body: "{}" })).rejects.toThrow("Field required; Invalid value");
  await expect(api("/sessions")).rejects.not.toThrow("[object Object]");
});
