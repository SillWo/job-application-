import { act, renderHook, waitFor } from '@testing-library/react'
import { useSessionDraft } from './useSessionDraft'

const key = 'job-orchestrator.session-draft'
const draft = { adapter: 'hirehi', applicationLimit: '7', desiredJobDescription: 'Исследования; не продажи', coverLetterAuto: true, coverLetterTemplate: '', unlimitedApplications: true, influence: { tasks: 'high', skills: 'low', experience_depth: 'medium', role_match: 'maximum', industry: 'low' } }
const response = (value: unknown, status = 200) => ({ ok: status === 200, status, json: async () => value })

beforeEach(() => localStorage.clear())
afterEach(() => vi.unstubAllGlobals())

test('retries a failed save without losing newer edits', async () => {
  vi.useFakeTimers()
  let attempts = 0
  const fetcher = vi.fn(async (_: unknown, options?: RequestInit) => {
    if (options?.method !== 'PUT') return response({ revision: 1, draft })
    attempts += 1
    if (attempts === 1) throw new Error('offline')
    return response({ revision: 2, draft: JSON.parse(String(options.body)).draft })
  })
  vi.stubGlobal('fetch', fetcher)
  const view = renderHook(useSessionDraft)
  try {
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    act(() => view.result.current.updateDraft({ desiredJobDescription: 'Первое' }))
    await act(async () => { await vi.advanceTimersByTimeAsync(500) })
    expect(view.result.current.status).toContain('Не удалось сохранить')
    expect(JSON.parse(localStorage.getItem(key)!).pending).toBe(true)
    act(() => view.result.current.updateDraft({ desiredJobDescription: 'Последнее' }))
    await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
    expect(attempts).toBe(2)
    expect(JSON.parse(String(fetcher.mock.calls.at(-1)?.[1]?.body)).draft.desiredJobDescription).toBe('Последнее')
    expect(JSON.parse(localStorage.getItem(key)!).pending).toBe(false)
  } finally { view.unmount(); vi.useRealTimers() }
})

test('serializes writes and flushes an edit made during an in-flight save when leaving', async () => {
  vi.useFakeTimers()
  let finish!: (value: unknown) => void
  const writes: Array<{ revision: number; draft: typeof draft }> = []
  vi.stubGlobal('fetch', vi.fn(async (_: unknown, options?: RequestInit) => {
    if (options?.method !== 'PUT') return response({ revision: 1, draft })
    const body = JSON.parse(String(options.body))
    writes.push(body)
    if (writes.length === 1) return new Promise((done) => { finish = done })
    return response({ revision: 3, draft: body.draft })
  }))
  const view = renderHook(useSessionDraft)
  try {
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    act(() => view.result.current.updateDraft({ desiredJobDescription: 'Первое' }))
    await act(async () => { await vi.advanceTimersByTimeAsync(500) })
    act(() => view.result.current.updateDraft({ desiredJobDescription: 'Последнее' }))
    view.unmount()
    await act(async () => { finish(response({ revision: 2, draft: writes[0].draft })); await vi.advanceTimersByTimeAsync(0) })
    expect(writes.map((value) => value.revision)).toEqual([1, 2])
    expect(writes[1].draft.desiredJobDescription).toBe('Последнее')
    expect(JSON.parse(localStorage.getItem(key)!).pending).toBe(false)
  } finally { vi.useRealTimers() }
})

test('restores all fields after browser storage is cleared and saves intentional empty text', async () => {
  let saved = { revision: 4, draft }
  vi.stubGlobal('fetch', vi.fn(async (_: unknown, options?: RequestInit) => {
    if (options?.method === 'PUT') {
      const body = JSON.parse(String(options.body))
      saved = { revision: body.revision + 1, draft: body.draft }
    }
    return response(saved)
  }))
  const first = renderHook(useSessionDraft)
  await waitFor(() => expect(first.result.current.draft).toEqual(draft))
  act(() => first.result.current.updateDraft({ desiredJobDescription: '' }))
  await waitFor(() => expect(saved.draft.desiredJobDescription).toBe(''))
  first.unmount()
  localStorage.clear()
  const next = renderHook(useSessionDraft)
  await waitFor(() => expect(next.result.current.status).toContain('сохранена'))
  expect(next.result.current.draft.desiredJobDescription).toBe('')
  expect(next.result.current.draft.applicationLimit).toBe('7')
})

test('slow hydration preserves typing while restoring other saved fields', async () => {
  let resolve!: (value: unknown) => void
  vi.stubGlobal('fetch', vi.fn(() => new Promise((done) => { resolve = done })))
  const view = renderHook(useSessionDraft)
  await waitFor(() => expect(resolve).toBeDefined())
  act(() => view.result.current.updateDraft({ desiredJobDescription: 'Печатаю сейчас' }))
  await act(async () => resolve(response({ revision: 2, draft })))
  expect(view.result.current.draft.desiredJobDescription).toBe('Печатаю сейчас')
  expect(view.result.current.draft.adapter).toBe('hirehi')
  // Complete the final save on unmount so subsequent mounts can read again.
  vi.stubGlobal('fetch', vi.fn(async () => response({ revision: 3, draft: view.result.current.draft })))
  view.unmount()
})

test('stale pending browser copy cannot overwrite the server copy', async () => {
  localStorage.setItem(key, JSON.stringify({ ...draft, desiredJobDescription: 'Старая вкладка', revision: 1, pending: true }))
  const fetcher = vi.fn(async () => response({ revision: 2, draft }))
  vi.stubGlobal('fetch', fetcher)
  const view = renderHook(useSessionDraft)
  await waitFor(() => expect(view.result.current.conflict).toBe(true))
  expect(view.result.current.draft.desiredJobDescription).toBe('Старая вкладка')
  expect(fetcher).toHaveBeenCalledTimes(1)
  await act(async () => view.result.current.loadSaved())
  expect(view.result.current.draft).toEqual(draft)
  expect(view.result.current.conflict).toBe(false)
})

test('server saves still work when browser storage is unavailable', async () => {
  const storage = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('blocked') })
  const fetcher = vi.fn(async (_: unknown, options?: RequestInit) => response({ revision: options?.method === 'PUT' ? 2 : 1, draft }))
  vi.stubGlobal('fetch', fetcher)
  const view = renderHook(useSessionDraft)
  await waitFor(() => expect(view.result.current.status).toContain('сохранена'))
  act(() => view.result.current.updateDraft({ desiredJobDescription: 'Новое' }))
  await waitFor(() => expect(fetcher.mock.calls.some(([, options]) => options?.method === 'PUT')).toBe(true))
  await waitFor(() => expect(view.result.current.status).toContain('сохранена'))
  storage.mockRestore()
})
