export async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const isFormData = typeof FormData !== 'undefined' && options?.body instanceof FormData;
  const headers: Record<string, string> = {};
  const suppliedHeaders = options?.headers;
  if (suppliedHeaders instanceof Headers) {
    suppliedHeaders.forEach((value, name) => { headers[name] = value; });
  } else if (Array.isArray(suppliedHeaders)) {
    for (const [name, value] of suppliedHeaders) headers[name] = value;
  } else if (suppliedHeaders) {
    Object.assign(headers, suppliedHeaders);
  }
  if (!isFormData && !Object.keys(headers).some((name) => name.toLowerCase() === 'content-type')) {
    headers['Content-Type'] = 'application/json';
  }

  const response = await fetch(`/api${path}`, { ...options, headers })
  if (!response.ok) {
    const payload = await response.json().catch(() => ({})) as { detail?: unknown };
    const detail = payload.detail;
    const message = typeof detail === 'string'
      ? detail
      : Array.isArray(detail)
        ? detail.map((item: unknown) => item && typeof item === 'object' && 'msg' in item && typeof item.msg === 'string' ? item.msg : '').filter(Boolean).join('; ') || 'Ошибка запроса'
        : 'Ошибка запроса';
    throw new Error(message);
  }
  // A create request may complete with 202 and an empty body while the
  // runtime is still materialising the session. Keep the transport helper
  // usable for those responses instead of turning a successful request into
  // a JSON parse failure.
  if (response.status === 204) return undefined as T;
  if (typeof response.text !== 'function') return response.json() as Promise<T>;
  const body = await response.text();
  if (!body.trim()) return undefined as T;
  return JSON.parse(body) as T;
}

