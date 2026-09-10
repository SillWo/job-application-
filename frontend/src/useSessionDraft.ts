import { useEffect, useRef, useState } from 'react'

const STORAGE_KEY = 'job-orchestrator.session-draft'
export type InfluenceLevel = 'low' | 'medium' | 'high' | 'maximum'
export type SessionDraft = {
  adapter: string
  applicationLimit: string
  desiredJobDescription: string
  coverLetterAuto: boolean
  coverLetterTemplate: string
  unlimitedApplications: boolean
  influence: Record<string, InfluenceLevel>
}
type CachedDraft = Partial<SessionDraft> & { revision?: number; pending?: boolean }
type SavedDraft = { revision: number; draft: SessionDraft | null }
const defaults: SessionDraft = {
  adapter: 'hh', applicationLimit: '5', desiredJobDescription: '', coverLetterAuto: true, coverLetterTemplate: '', unlimitedApplications: false,
  influence: { tasks: 'medium', skills: 'low', experience_depth: 'medium', role_match: 'medium', industry: 'medium' },
}

function readLocal(): CachedDraft {
  try {
    const value = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}')
    const draft: CachedDraft = {}
    if (!value || typeof value !== 'object') return draft
    if (['hh', 'hirehi', 'zarplata'].includes(value.adapter)) draft.adapter = value.adapter
    for (const key of ['applicationLimit', 'desiredJobDescription', 'coverLetterTemplate'] as const) {
      if (typeof value[key] === 'string') draft[key] = value[key].slice(0, key === 'applicationLimit' ? 32 : key === 'coverLetterTemplate' ? 12000 : 2000)
    }
    if (typeof value.coverLetterAuto === 'boolean') draft.coverLetterAuto = value.coverLetterAuto
    if (typeof value.unlimitedApplications === 'boolean') draft.unlimitedApplications = value.unlimitedApplications
    if (Number.isInteger(value.revision) && value.revision >= 0) draft.revision = value.revision
    if (typeof value.pending === 'boolean') draft.pending = value.pending
    if (value.influence && typeof value.influence === 'object') {
      draft.influence = { ...defaults.influence }
      for (const key of Object.keys(defaults.influence)) {
        const levels = key === 'skills' ? ['low', 'medium'] : ['low', 'medium', 'high', 'maximum']
        if (levels.includes(value.influence[key])) draft.influence[key] = value.influence[key]
      }
    }
    return draft
  } catch { return {} }
}

function fields(value: CachedDraft): Partial<SessionDraft> {
  const { revision: _revision, pending: _pending, ...draft } = value
  void _revision; void _pending
  return draft
}

function writeLocal(draft: SessionDraft, revision: number | undefined, pending: boolean) {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify({ ...draft, revision, pending })) } catch { /* Server copy remains available. */ }
}

// A route remount waits for the previous form's final save before reading.
let serverWork: Promise<unknown> = Promise.resolve()

async function request(options?: RequestInit): Promise<SavedDraft> {
  const response = await fetch('/api/session-draft', { headers: { 'Content-Type': 'application/json' }, ...options })
  if (!response.ok) throw new Error(response.status === 409 ? 'conflict' : 'unavailable')
  const result = await response.json() as SavedDraft
  if (!Number.isInteger(result.revision) || !('draft' in result)) throw new Error('unavailable')
  return result
}

export function useSessionDraft() {
  const [initial] = useState(readLocal)
  const [draft, setDraft] = useState<SessionDraft>(() => ({ ...defaults, ...fields(initial) }))
  const [status, setStatus] = useState('Загрузка сохранённой формы…')
  const [conflict, setConflict] = useState(false)
  const current = useRef(draft)
  const edited = useRef<Partial<SessionDraft>>({})
  const revision = useRef(initial.revision)
  const dirty = useRef(initial.pending === true)
  const hydrated = useRef(false)
  const blocked = useRef(false)
  const generation = useRef(0)

  const updateDraft = (patch: Partial<SessionDraft>) => {
    current.current = { ...current.current, ...patch }
    edited.current = { ...edited.current, ...patch }
    generation.current += 1
    dirty.current = true
    setDraft(current.current)
    writeLocal(current.current, revision.current, true)
    if (!blocked.current) setStatus('Сохранение формы…')
  }

  useEffect(() => {
    let active = true
    let running = false
    let retryAt = 0
    const report = (text: string) => { if (active) setStatus(text) }
    const markConflict = () => {
      blocked.current = true
      if (active) setConflict(true)
      report('Форма изменена в другой вкладке. Ваш текст остался в этом браузере; сохранённая форма не перезаписана.')
    }
    const load = async () => {
      running = true
      try {
        await serverWork
        const saved = await request()
        if (!active) return
        if (initial.pending && initial.revision !== undefined && initial.revision !== saved.revision) {
          markConflict()
          return
        }
        // Migrate old browser drafts once. On later visits the server wins,
        // except for edits which have not reached it yet.
        const local = initial.pending || (saved.revision === 0) ? fields(initial) : {}
        if (initial.pending === undefined && !local.desiredJobDescription && saved.draft?.desiredJobDescription) {
          delete local.desiredJobDescription
        }
        current.current = { ...defaults, ...saved.draft, ...local, ...edited.current }
        revision.current = saved.revision
        dirty.current = initial.pending === true || generation.current > 0 || saved.revision === 0
        hydrated.current = true
        setDraft(current.current)
        writeLocal(current.current, saved.revision, dirty.current)
        report(dirty.current ? 'Сохранение формы…' : 'Форма сохранена на этом компьютере')
      } catch {
        retryAt = Date.now() + 5000
        report('Не удалось загрузить сохранённую форму. Повторим автоматически; введённый текст остаётся в браузере.')
      } finally { running = false }
    }
    const save = () => {
      if (running || !hydrated.current || !dirty.current || blocked.current) return
      running = true
      const snapshot = current.current
      const version = generation.current
      serverWork = serverWork.then(async () => {
        let savedSuccessfully = false
        try {
          const saved = await request({ method: 'PUT', keepalive: true, body: JSON.stringify({ revision: revision.current, draft: snapshot }) })
          revision.current = saved.revision
          dirty.current = generation.current !== version
          writeLocal(current.current, saved.revision, dirty.current)
          savedSuccessfully = true
          report(dirty.current ? 'Сохранение формы…' : 'Форма сохранена на этом компьютере')
        } catch (error) {
          if (error instanceof Error && error.message === 'conflict') markConflict()
          else {
            retryAt = Date.now() + 5000
            report('Не удалось сохранить форму на компьютере. Повторим автоматически; текст остаётся в браузере.')
          }
        } finally {
          running = false
          // An edit may have arrived during the last request, just before leaving.
          if (!active && dirty.current && savedSuccessfully) save()
        }
      })
    }
    void load()
    const timer = window.setInterval(() => {
      if (running || blocked.current || Date.now() < retryAt) return
      if (!hydrated.current) void load()
      else save()
    }, 500)
    const flush = () => save()
    window.addEventListener('pagehide', flush)
    return () => {
      active = false
      window.clearInterval(timer)
      window.removeEventListener('pagehide', flush)
      save()
    }
  }, [initial])

  const loadSaved = async () => {
    try {
      await serverWork
      const saved = await request()
      current.current = { ...defaults, ...saved.draft }
      revision.current = saved.revision
      dirty.current = false
      blocked.current = false
      hydrated.current = true
      edited.current = {}
      setDraft(current.current)
      writeLocal(current.current, saved.revision, false)
      setConflict(false)
      setStatus('Форма загружена с этого компьютера')
    } catch { setStatus('Не удалось загрузить форму. Попробуйте ещё раз.') }
  }

  return { draft, updateDraft, status, conflict, loadSaved }
}
