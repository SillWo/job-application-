export async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, { headers: options?.body instanceof FormData ? undefined : { 'Content-Type': 'application/json' }, ...options })
  if (!response.ok) { const detail = await response.json().catch(() => ({ detail: response.statusText })); throw new Error(detail.detail ?? 'Ошибка запроса') }
  return response.json() as Promise<T>
}

