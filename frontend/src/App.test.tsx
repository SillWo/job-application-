import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import App from './App'
import { Notifications } from './App'

beforeEach(() => { vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => [] })) })
afterEach(() => vi.unstubAllGlobals())

test('renders the Russian dashboard', async () => {
  render(<QueryClientProvider client={new QueryClient({defaultOptions:{queries:{retry:false}}})}><MemoryRouter><App /></MemoryRouter></QueryClientProvider>)
  expect(screen.getByText('Ваш поиск работы — под контролем')).toBeInTheDocument()
  expect(screen.queryByText('Приватность под контролем')).not.toBeInTheDocument()
  expect(screen.queryByText('Облачная модель недоступна')).not.toBeInTheDocument()
  expect(screen.getByRole('link', { name: 'API' })).toBeInTheDocument()
  expect(screen.getByText(/Ваш запрос на естественном языке учитывается/)).toBeInTheDocument()
  expect(screen.queryByText(/Сначала фильтры/)).not.toBeInTheDocument()
})

test('places notifications before API in the topbar actions', () => {
  const { container } = render(<QueryClientProvider client={new QueryClient({defaultOptions:{queries:{retry:false}}})}><MemoryRouter><App /></MemoryRouter></QueryClientProvider>)
  const actions = container.querySelector('.topbar-actions')
  expect(actions).not.toBeNull()
  const bell = within(actions as HTMLElement).getByRole('button', { name: 'Уведомления' })
  const apiLink = within(actions as HTMLElement).getByRole('link', { name: 'API' })
  expect(Array.from(actions!.children).indexOf(bell)).toBeLessThan(Array.from(actions!.children).indexOf(apiLink))
})

test('shows notifications, marks one read, navigates, and reads all', async () => {
  const requests: Array<{ url: string; method: string }> = []
  const notification = { id: 1, kind: 'session', title: 'Сессия запущена', message: 'Сессия #4 запущена', source_type: 'session', source_id: '4', target_path: '/session', read_at: null, created_at: '2026-08-22T10:00:00Z' }
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input); requests.push({ url, method: init?.method ?? 'GET' })
    if (url.endsWith('/api/notifications')) return Promise.resolve({ ok: true, json: async () => [notification] })
    return Promise.resolve({ ok: true, json: async () => [] })
  }))
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter><App /></MemoryRouter></QueryClientProvider>)
  const bell = await screen.findByRole('button', { name: 'Уведомления' })
  expect(bell.querySelector('svg.notification-icon')).toBeInTheDocument()
  expect(bell).not.toHaveTextContent('🔔')
  await waitFor(() => expect(screen.getByRole('status', { name: '1 непрочитанных' })).toBeInTheDocument())
  fireEvent.click(bell)
  expect(await screen.findByText('Сессия запущена')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: /Сессия запущена/ }))
  await waitFor(() => expect(requests).toContainEqual({ url: '/api/notifications/1/read', method: 'PATCH' }))
  await waitFor(() => expect(screen.getByRole('link', { name: 'Сессия' })).toHaveClass('active'))
  fireEvent.click(bell)
  fireEvent.click(screen.getByRole('button', { name: 'Прочитать все' }))
  await waitFor(() => expect(requests).toContainEqual({ url: '/api/notifications/read-all', method: 'POST' }))
})

test('signals only for newly arrived notification IDs', async () => {
  const payloads = [
    [{ id: 1, kind: 'session', title: 'A', message: 'A', source_type: 'session', source_id: '1', target_path: '/session', read_at: null, created_at: '2026-08-22T10:00:00Z' }],
    [{ id: 1, kind: 'session', title: 'A', message: 'A', source_type: 'session', source_id: '1', target_path: '/session', read_at: null, created_at: '2026-08-22T10:00:00Z' }, { id: 2, kind: 'session', title: 'B', message: 'B', source_type: 'session', source_id: '2', target_path: '/session', read_at: null, created_at: '2026-08-22T10:01:00Z' }],
    [{ id: 1, kind: 'session', title: 'A', message: 'A', source_type: 'session', source_id: '1', target_path: '/session', read_at: null, created_at: '2026-08-22T10:00:00Z' }, { id: 2, kind: 'session', title: 'B', message: 'B', source_type: 'session', source_id: '2', target_path: '/session', read_at: null, created_at: '2026-08-22T10:01:00Z' }],
  ]
  let call = 0
  const contexts = { created: 0 }
  class MockAudioContext { currentTime = 0; destination = {}; createOscillator() { return { frequency: { value: 0 }, connect() {}, start() {}, stop() {}, onended: null as (() => void) | null } } createGain() { return { gain: { setValueAtTime() {}, exponentialRampToValueAtTime() {} }, connect() {} } } close() { return Promise.resolve() } }
  vi.stubGlobal('AudioContext', class extends MockAudioContext { constructor() { super(); contexts.created += 1 } })
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => String(input).endsWith('/api/notifications') ? Promise.resolve({ ok: true, json: async () => payloads[Math.min(call++, payloads.length - 1)] }) : Promise.resolve({ ok: true, json: async () => ({}) })))
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><MemoryRouter><Notifications /></MemoryRouter></QueryClientProvider>)
  await waitFor(() => expect(screen.getByRole('status', { name: '1 непрочитанных' })).toBeInTheDocument())
  expect(contexts.created).toBe(0)
  fireEvent.click(screen.getByRole('button', { name: 'Уведомления' }))
  expect(screen.getAllByText('A').length).toBeGreaterThan(0)
  await client.refetchQueries({ queryKey: ['notifications'] })
  await waitFor(() => expect(contexts.created).toBe(1))
  await client.refetchQueries({ queryKey: ['notifications'] })
  expect(contexts.created).toBe(1)
})

test('RUNNING HH does not block launching HireHi', async () => {
  const requests: Array<{ url: string; method?: string; body?: string }> = []
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input); requests.push({ url, method: init?.method, body: init?.body ? String(init.body) : undefined })
    if (url.endsWith('/api/adapters')) return Promise.resolve({ ok: true, json: async () => [{ site_id: 'hh', display_name: 'HH.ru' }, { site_id: 'hirehi', display_name: 'HireHi' }] })
    if (url.endsWith('/api/profiles')) return Promise.resolve({ ok: true, json: async () => [{ id: 1, data: {} }] })
    if (url.endsWith('/api/profiles/1/resumes')) return Promise.resolve({ ok: true, json: async () => [{ id: 1, selected_for_matching: true }] })
    if (url.endsWith('/api/sessions') && init?.method === 'POST') return Promise.resolve({ ok: true, json: async () => ({ id: 22, profile_id: 1, adapter_id: 'hirehi', status: 'CREATED', counters: {} }) })
    if (url.endsWith('/api/sessions/22/start')) return Promise.resolve({ ok: true, json: async () => ({ ok: true }) })
    if (url.endsWith('/api/sessions')) return Promise.resolve({ ok: true, json: async () => [{ id: 11, profile_id: 1, adapter_id: 'hh', status: 'RUNNING', counters: {} }] })
    return Promise.resolve({ ok: true, json: async () => [] })
  }))
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter initialEntries={['/session']}><App /></MemoryRouter></QueryClientProvider>)
  const select = await screen.findByRole('combobox', { name: 'Сайт' })
  await screen.findByRole('option', { name: 'HireHi' })
  fireEvent.change(select, { target: { value: 'hirehi' } })
  const launch = screen.getByRole('button', { name: 'Создать и запустить' })
  await waitFor(() => expect(launch).toBeEnabled())
  fireEvent.click(launch)
  await waitFor(() => expect(requests.some((r) => r.url.endsWith('/api/sessions/22/start'))).toBe(true))
  const create = requests.find((r) => r.url.endsWith('/api/sessions') && r.method === 'POST')
  expect(JSON.parse(create?.body ?? '{}')).toMatchObject({ adapter_id: 'hirehi' })
})

test('stops only the selected session card', async () => {
  const requests: string[] = []
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input); requests.push(`${init?.method ?? 'GET'} ${url}`)
    if (url.endsWith('/api/sessions')) return Promise.resolve({ ok: true, json: async () => [
      { id: 11, profile_id: 1, adapter_id: 'hh', status: 'RUNNING', counters: {} },
      { id: 22, profile_id: 1, adapter_id: 'hirehi', status: 'RUNNING', counters: {} },
    ] })
    if (url.endsWith('/api/sessions/22/report')) return Promise.resolve({ ok: true, json: async () => ({ ready: false, pdf_url: null }) })
    if (url.endsWith('/api/sessions/11/stop')) return Promise.resolve({ ok: true, json: async () => ({}) })
    return Promise.resolve({ ok: true, json: async () => [] })
  }))
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter initialEntries={['/session']}><App /></MemoryRouter></QueryClientProvider>)
  const card = await screen.findByTestId('session-11')
  fireEvent.click(within(card).getByRole('button', { name: 'Остановить' }))
  await waitFor(() => expect(requests).toContain('POST /api/sessions/11/stop'))
  expect(requests.some((r) => r.includes('/api/sessions/22/stop'))).toBe(false)
})

test('HH launch has no per-session resume controls', async () => {
  const requests: Array<{ url: string; body?: string }> = []
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    requests.push({ url, body: init?.body ? String(init.body) : undefined })
    if (url.endsWith('/api/profiles')) return Promise.resolve({ ok: true, json: async () => [{ id: 1, data: {} }] })
    if (url.endsWith('/api/profiles/1/resumes')) return Promise.resolve({ ok: true, json: async () => [{ id: 1, selected_for_matching: true }] })
    if (url.endsWith('/api/sessions') && init?.method === 'POST') return Promise.resolve({ ok: true, json: async () => ({ id: 2, profile_id: 1, adapter_id: 'hh', status: 'CREATED', counters: {} }) })
    if (url.endsWith('/api/sessions/2/start')) return Promise.resolve({ ok: true, json: async () => ({ ok: true }) })
    return Promise.resolve({ ok: true, json: async () => [] })
  }))
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter initialEntries={['/session']}><App /></MemoryRouter></QueryClientProvider>)
  const launch = await screen.findByRole('button', { name: 'Создать и запустить' })
  expect(screen.queryByLabelText('Публичная ссылка на резюме (необязательно)')).not.toBeInTheDocument()
  expect(screen.queryByLabelText('Загрузить резюме при запуске')).not.toBeInTheDocument()
  await waitFor(() => expect(launch).toBeEnabled())
  fireEvent.click(launch)
  await waitFor(() => expect(requests.some((request) => request.url.endsWith('/api/sessions/2/start'))).toBe(true))
  const payload = requests.find((request) => request.url.endsWith('/api/sessions') && request.body)
  expect(JSON.parse(payload?.body ?? '{}')).not.toHaveProperty('resume_url')
  expect(requests.some((request) => request.url.endsWith('/resume-file'))).toBe(false)
})

test('exposes every primary route through keyboard-accessible navigation', () => {
  render(<QueryClientProvider client={new QueryClient({defaultOptions:{queries:{retry:false}}})}><MemoryRouter><App /></MemoryRouter></QueryClientProvider>)
  const navigation = screen.getByRole('navigation', { name: 'Основная навигация' })
  expect(navigation).toHaveClass('topbar-nav')
  const shell = navigation.closest('.shell') as HTMLElement
  expect(shell.querySelector(':scope > .topbar')).toBeInTheDocument()
  expect(shell.querySelector(':scope > main')).toBeInTheDocument()
  expect(screen.queryByRole('complementary')).not.toBeInTheDocument()
  const expected = [
    ['Обзор', '/'],
    ['Профиль', '/profile'],
    ['Сайты', '/sites'],
    ['Сессия', '/session'],
    ['Вакансии', '/vacancies'],
    ['Модель', '/model'],
  ]
  for (const [name, href] of expected) {
    expect(screen.getByRole('link', { name })).toHaveAttribute('href', href)
  }
  expect(screen.queryByRole('link', { name: 'Проверка' })).not.toBeInTheDocument()
  expect(screen.queryByRole('link', { name: 'Отчёты' })).not.toBeInTheDocument()
})

test('session shows only the four requested counters', async () => {
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.endsWith('/api/sessions')) return Promise.resolve({ ok: true, json: async () => [{ id: 9, profile_id: 1, adapter_id: 'hh', status: 'COMPLETED', counters: { viewed: 30, filtered: 12, submitted: 5, errors: 2, matched: 18, already_applied: 3, review: 1, skipped_test: 2 }, started_at: null, finished_at: null, stop_reason: null }] })
    return Promise.resolve({ ok: true, json: async () => ({ connected: true, model_available: true, model: 'test' }) })
  }))
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter initialEntries={['/session']}><App /></MemoryRouter></QueryClientProvider>)
  const stats = await screen.findByText('Просмотрено')
  const container = stats.closest('.stats')
  expect(container).not.toBeNull()
  expect(within(container as HTMLElement).getAllByRole('article')).toHaveLength(4)
  for (const label of ['Просмотрено', 'Отфильтровано', 'Отклики', 'Ошибка']) expect(within(container as HTMLElement).getByText(label)).toBeInTheDocument()
  for (const label of ['Уже откликались', 'Тестовые', 'Проверка', 'Совпадения', 'Ошибки']) expect(within(container as HTMLElement).queryByText(label)).not.toBeInTheDocument()
})

test('groups terminal sessions behind a collapsed history block', async () => {
  const sessions = ['CREATED', 'RUNNING', 'WAITING_FOR_LOGIN', 'PAUSED', 'STOPPED', 'COMPLETED', 'FAILED'].map((status, index) => ({
    id: index + 1, profile_id: 1, adapter_id: 'hh', status, counters: {}, started_at: null, finished_at: null, stop_reason: null,
  }))
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    if (String(input).endsWith('/api/sessions')) return Promise.resolve({ ok: true, json: async () => sessions })
    return Promise.resolve({ ok: true, json: async () => [] })
  }))
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter initialEntries={['/session']}><App /></MemoryRouter></QueryClientProvider>)
  for (const id of [1, 2, 3, 4]) expect(await screen.findByTestId(`session-${id}`)).toBeVisible()
  const history = await screen.findByText('Завершённые сессии (3)')
  const details = history.closest('details') as HTMLDetailsElement
  expect(details).not.toHaveAttribute('open')
  for (const id of [5, 6, 7]) expect(screen.getByTestId(`session-${id}`)).not.toBeVisible()
  fireEvent.click(history)
  for (const id of [5, 6, 7]) expect(screen.getByTestId(`session-${id}`)).toBeVisible()
})

test('does not show terminal history when there are no terminal sessions', async () => {
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    if (String(input).endsWith('/api/sessions')) return Promise.resolve({ ok: true, json: async () => [{ id: 1, profile_id: 1, adapter_id: 'hh', status: 'RUNNING', counters: {} }] })
    return Promise.resolve({ ok: true, json: async () => [] })
  }))
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter initialEntries={['/session']}><App /></MemoryRouter></QueryClientProvider>)
  await screen.findByTestId('session-1')
  expect(screen.queryByText(/Завершённые сессии/)).not.toBeInTheDocument()
})

test('HireHi launch hides per-session resume controls', async () => {
  const requests: Array<{ url: string; body?: string }> = []
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    requests.push({ url, body: init?.body ? String(init.body) : undefined })
    if (url.endsWith('/api/adapters')) return Promise.resolve({ ok: true, json: async () => [{ site_id: 'hh', display_name: 'HH.ru', allowed_domains: ['hh.ru'] }, { site_id: 'hirehi', display_name: 'HireHi', allowed_domains: ['hirehi.ru'] }] })
    if (url.endsWith('/api/profiles')) return Promise.resolve({ ok: true, json: async () => [{ id: 1, data: {}, created_at: '2026-08-13T00:00:00Z' }] })
    if (url.endsWith('/api/profiles/1/resumes')) return Promise.resolve({ ok: true, json: async () => [{ id: 1, profile_id: 1, name: 'Resume', selected_for_matching: true, skills: [], experiences: [] }] })
    if (url.endsWith('/api/sessions') && init?.method === 'POST') return Promise.resolve({ ok: true, json: async () => ({ id: 3, profile_id: 1, adapter_id: 'hirehi', status: 'CREATED', counters: {} }) })
    if (url.endsWith('/api/sessions')) return Promise.resolve({ ok: true, json: async () => [] })
    if (url.endsWith('/api/sessions/3/start')) return Promise.resolve({ ok: true, json: async () => ({ ok: true }) })
    return Promise.resolve({ ok: true, json: async () => [] })
  }))
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter initialEntries={['/session']}><App /></MemoryRouter></QueryClientProvider>)
  const select = await screen.findByRole('combobox', { name: 'Сайт' })
  await within(select).findByRole('option', { name: 'HireHi' })
  expect(within(select).getAllByRole('option', { name: 'HH.ru' })).toHaveLength(1)
  fireEvent.change(select, { target: { value: 'hirehi' } })
  await waitFor(() => expect(select).toHaveValue('hirehi'))
  expect(screen.queryByLabelText('Публичная ссылка на резюме (необязательно)')).not.toBeInTheDocument()
  expect(screen.queryByLabelText('Загрузить резюме при запуске')).not.toBeInTheDocument()
  const launch = screen.getByRole('button', { name: 'Создать и запустить' })
  await waitFor(() => expect(launch).toBeEnabled())
  fireEvent.click(launch)
  await waitFor(() => expect(requests.some((request) => request.url.endsWith('/api/sessions') && request.body)).toBe(true))
  expect(requests.some((request) => request.url.endsWith('/resume-file'))).toBe(false)
  const payload = requests.find((request) => request.url.endsWith('/api/sessions') && request.body)
  expect(JSON.parse(payload?.body ?? '{}')).not.toHaveProperty('resume_url')
  await waitFor(() => expect(requests.some((request) => request.url.endsWith('/api/sessions/3/start'))).toBe(true))
})

test('shows deterministic ready HireHi PDF link', async () => {
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.endsWith('/api/sessions')) return Promise.resolve({ ok: true, json: async () => [{ id: 7, profile_id: 1, adapter_id: 'hirehi', status: 'COMPLETED', counters: {}, started_at: null, finished_at: null, stop_reason: null }] })
    if (url.endsWith('/api/sessions/7/report')) return Promise.resolve({ ok: true, json: async () => ({ ready: true, pdf_url: '/api/sessions/7/report/pdf' }) })
    return Promise.resolve({ ok: true, json: async () => [] })
  }))
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter initialEntries={['/session']}><App /></MemoryRouter></QueryClientProvider>)
  fireEvent.click(await screen.findByText('Завершённые сессии (1)'))
  const reportLink = await screen.findByRole('link', { name: 'Скачать PDF HireHi #7' })
  expect(reportLink).toHaveTextContent('Скачать PDF-отчёт')
  expect(reportLink).toHaveClass('button-link', 'session-report-button')
  expect(reportLink.querySelector('.session-report-icon')).not.toBeNull()
  expect(reportLink).toHaveAttribute('href', '/api/sessions/7/report/pdf')
})

test('shows the latest completed HireHi report while a newer HH session is running', async () => {
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.endsWith('/api/sessions')) return Promise.resolve({ ok: true, json: async () => [
      { id: 12, profile_id: 1, adapter_id: 'hh', status: 'RUNNING', counters: {}, started_at: null, finished_at: null, stop_reason: null },
      { id: 9, profile_id: 1, adapter_id: 'hirehi', status: 'STOPPED', counters: {}, started_at: null, finished_at: null, stop_reason: null },
    ] })
    if (url.endsWith('/api/sessions/9/report')) return Promise.resolve({ ok: true, json: async () => ({ ready: true, pdf_url: '/api/sessions/9/report/pdf' }) })
    return Promise.resolve({ ok: true, json: async () => [] })
  }))
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter initialEntries={['/session']}><App /></MemoryRouter></QueryClientProvider>)
  fireEvent.click(await screen.findByText('Завершённые сессии (1)'))
  expect(await screen.findByRole('link', { name: 'Скачать PDF HireHi #9' })).toHaveAttribute('href', '/api/sessions/9/report/pdf')
})

test('shows resume import progress and creates a separate resume', async () => {
  let finishUpload!: (response: { ok: boolean; json: () => Promise<unknown> }) => void
  const uploadResponse = new Promise<{ ok: boolean; json: () => Promise<unknown> }>((resolve) => {
    finishUpload = resolve
  })
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.endsWith('/api/profiles/1/resumes/import')) return uploadResponse
    if (url.endsWith('/api/model/status')) {
      return Promise.resolve({ ok: true, json: async () => ({ connected: true, model_available: true, model: 'test' }) })
    }
    if (url.endsWith('/api/profiles')) return Promise.resolve({ ok: true, json: async () => [{ id: 1, data: { full_name: 'Sample Candidate', residence: '', job_search_locations: [], contacts: { phone: null, email: null, messengers: [] }, education: [], languages: [], driver_license: false }, created_at: '2026-08-13T00:00:00Z' }] })
    if (url.endsWith('/api/profiles/1/resumes')) return Promise.resolve({ ok: true, json: async () => [] })
    return Promise.resolve({ ok: true, json: async () => [] })
  }))

  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter initialEntries={['/profile']}><App /></MemoryRouter>
    </QueryClientProvider>,
  )

  const input = await screen.findByLabelText('Импортировать резюме')
  fireEvent.change(input, { target: { files: [new File(['Sample Candidate'], 'resume.txt', { type: 'text/plain' })] } })

  expect(await screen.findByText('Обрабатываем «resume.txt»…')).toBeInTheDocument()
  expect(screen.getByText(/Файл «resume.txt» принят/)).toBeInTheDocument()

  finishUpload({
    ok: true,
    json: async () => ({
      profile: { id: 1, data: { full_name: 'Sample Candidate', residence: '', job_search_locations: [], contacts: { phone: null, email: null, messengers: [] }, education: [], languages: [], driver_license: false }, created_at: '2026-08-13T00:00:00Z' },
      resume: { id: 42, profile_id: 1, name: 'Product Manager', desired_title: 'Product Manager', desired_salary: '', employment_types: [], work_formats: [], business_trips: null, experiences: [], skills: [], about: '', selected_for_matching: false, original_filename: 'resume.txt' },
    }),
  })

  expect(await screen.findByDisplayValue('Sample Candidate')).toBeInTheDocument()
  expect(await screen.findByText(/Импорт «Product Manager» завершён/)).toBeInTheDocument()
  expect(screen.getAllByText('Product Manager').length).toBeGreaterThan(0)
})

test('renders structured personal profile and makes selected resumes explicit', async () => {
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    void init
    const url = String(input)
    if (url.endsWith('/api/model/status')) return Promise.resolve({ ok: true, json: async () => ({ connected: true, model_available: true, model: 'test' }) })
    if (url.endsWith('/api/profiles/1/resumes')) return Promise.resolve({ ok: true, json: async () => [{ id: 4, profile_id: 1, name: 'Product role', desired_title: 'Product Manager', desired_salary: '180000', employment_types: ['permanent'], work_formats: ['remote'], business_trips: false, experiences: [], skills: ['CustDev', 'Scrum'], about: 'Product work', selected_for_matching: true }] })
    if (url.endsWith('/api/profiles')) return Promise.resolve({ ok: true, json: async () => [{ id: 1, data: { full_name: 'Test Candidate', residence: 'Test City', job_search_locations: ['Remote'], contacts: { phone: null, email: 'candidate@example.test', messengers: [] }, education: [], languages: [], driver_license: false }, created_at: '2026-08-13T00:00:00Z' }] })
    return Promise.resolve({ ok: true, json: async () => [] })
  })
  vi.stubGlobal('fetch', fetchMock)
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter initialEntries={['/profile']}><App /></MemoryRouter></QueryClientProvider>)

  expect(await screen.findByText('Личная информация')).toBeInTheDocument()
  expect(screen.getByDisplayValue('Test Candidate')).toBeInTheDocument()
  expect(screen.getByRole('checkbox', { name: 'Передавать модели' })).toBeChecked()
  expect(screen.getAllByText('Product role').length).toBeGreaterThan(0)

  fireEvent.click(screen.getByRole('button', { name: '+ Образование' }))
  expect(screen.getByLabelText('Тип образования 1')).toBeInTheDocument()
  fireEvent.change(screen.getByLabelText('Тип образования 1'), { target: { value: 'school' } })
  expect(screen.getByText('Учебное заведение')).toBeInTheDocument()
  expect(screen.getByLabelText('Тип образования 1').closest('.profile-remove-row')).toBeTruthy()
  expect(screen.getByLabelText('Учебное заведение').closest('.profile-full-field')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: '+ Мессенджер' }))
  expect(screen.getByLabelText('Мессенджер 1')).toBeInTheDocument()
  expect(screen.getByLabelText('Мессенджер 1').closest('.profile-remove-row')).toBeTruthy()

  fireEvent.click(screen.getByRole('button', { name: 'Редактировать' }))
  const formatSelect = screen.getByRole('button', { name: 'Формат работы' })
  expect(formatSelect).toHaveAttribute('aria-expanded', 'false')
  expect(formatSelect).toHaveTextContent('Удалённо')
  fireEvent.click(formatSelect)
  expect(formatSelect).toHaveAttribute('aria-expanded', 'true')
  expect(screen.getByRole('checkbox', { name: 'Удалённо' })).toBeChecked()
  fireEvent.click(screen.getByRole('checkbox', { name: 'Гибрид' }))
  fireEvent.click(screen.getByRole('button', { name: 'Выбрать' }))
  expect(formatSelect).toHaveAttribute('aria-expanded', 'false')
  expect(formatSelect).toHaveTextContent('Гибрид, Удалённо')
  const about = screen.getByRole('textbox', { name: /О себе/ })
  fireEvent.change(about, { target: { value: 'Одно два три' } })
  expect(screen.getByText('3 / 500 слов')).toBeInTheDocument()

  fireEvent.click(screen.getByRole('button', { name: 'Сохранить резюме' }))
  await waitFor(() => expect(fetchMock.mock.calls.some(([input, init]) => String(input).endsWith('/api/profiles/1/resumes/4') && init?.method === 'PATCH')).toBe(true))
  const editResumeCall = fetchMock.mock.calls.find(([input, init]) => String(input).endsWith('/api/profiles/1/resumes/4') && init?.method === 'PATCH')
  expect(JSON.parse(String(editResumeCall?.[1]?.body))).toMatchObject({ work_formats: ['remote', 'hybrid'], about: 'Одно два три' })

  fireEvent.click(screen.getByRole('button', { name: 'Сохранить личный профиль' }))
  await waitFor(() => expect(fetchMock.mock.calls.some(([input, init]) => String(input).endsWith('/api/profiles/1') && init?.method === 'PATCH')).toBe(true))
  const profileCall = fetchMock.mock.calls.find(([input, init]) => String(input).endsWith('/api/profiles/1') && init?.method === 'PATCH')
  expect(JSON.parse(String(profileCall?.[1]?.body))).toMatchObject({
    full_name: 'Test Candidate',
    job_search_locations: ['Remote'],
    contacts: { email: 'candidate@example.test', messengers: [] },
    education: [{ type: 'school', institution: '', start_date: null, end_date: null }],
  })
  expect(JSON.parse(String(profileCall?.[1]?.body))).not.toHaveProperty('personal')

  fireEvent.click(screen.getByRole('checkbox', { name: 'Передавать модели' }))
  await waitFor(() => expect(fetchMock.mock.calls.some(([input, init]) => String(input).endsWith('/api/profiles/1/resumes/4') && init?.method === 'PATCH')).toBe(true))
  const resumeCall = fetchMock.mock.calls.find(([input, init]) => String(input).endsWith('/api/profiles/1/resumes/4') && init?.method === 'PATCH' && String(init.body).includes('"selected_for_matching":false'))
  expect(JSON.parse(String(resumeCall?.[1]?.body))).toMatchObject({ work_formats: ['remote'], selected_for_matching: false })
})

test('allows creating an HH session after a stopped session', async () => {
  const stoppedSession = {
    id: 1,
    profile_id: 1,
    adapter_id: 'hh',
    status: 'STOPPED',
    counters: {},
    started_at: null,
    finished_at: null,
    stop_reason: 'Остановлено пользователем',
  }
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.endsWith('/api/model/status')) {
      return Promise.resolve({ ok: true, json: async () => ({ connected: true, model_available: true, model: 'test' }) })
    }
    if (url.endsWith('/api/profiles')) return Promise.resolve({ ok: true, json: async () => [{ id: 1, data: { full_name: '', residence: '', job_search_locations: [], contacts: { phone: null, email: null, messengers: [] }, education: [], languages: [], driver_license: false }, created_at: '2026-08-13T00:00:00Z' }] })
    if (url.endsWith('/api/profiles/1/resumes')) return Promise.resolve({ ok: true, json: async () => [{ id: 7, profile_id: 1, name: 'Selected', desired_title: 'Role', desired_salary: null, employment_types: [], work_formats: [], business_trips: null, experiences: [], skills: [], about: '', selected_for_matching: true }] })
    if (url.endsWith('/api/sessions') && init?.method === 'POST') {
      return Promise.resolve({ ok: true, json: async () => ({ ...stoppedSession, id: 2, adapter_id: 'hh', status: 'CREATED' }) })
    }
    if (url.endsWith('/api/sessions')) return Promise.resolve({ ok: true, json: async () => [stoppedSession] })
    if (url.endsWith('/api/sessions/2/start')) return Promise.resolve({ ok: true, json: async () => ({ ok: true }) })
    return Promise.resolve({ ok: true, json: async () => [] })
  })
  vi.stubGlobal('fetch', fetchMock)

  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter initialEntries={['/session']}><App /></MemoryRouter>
    </QueryClientProvider>,
  )

  const createButton = await screen.findByRole('button', { name: 'Создать и запустить' })
  await waitFor(() => expect(createButton).toBeEnabled())
  fireEvent.click(screen.getByRole('checkbox', { name: 'Без ограничений: просмотр вакансий' }))
  fireEvent.click(screen.getByRole('checkbox', { name: 'Без ограничений: отправка откликов' }))
  fireEvent.click(createButton)

  expect(await screen.findByText(/Сессия #2 запущена/)).toBeInTheDocument()
  expect(fetchMock).toHaveBeenCalledWith(
    '/api/sessions/2/start',
    expect.objectContaining({ method: 'POST' }),
  )
  const createCall = fetchMock.mock.calls.find(([url, init]) => String(url).endsWith('/api/sessions') && init?.method === 'POST')
  expect(JSON.parse(String(createCall?.[1]?.body))).toMatchObject({ viewed_limit: null, application_limit: null })
  expect(JSON.parse(String(createCall?.[1]?.body))).toMatchObject({ profile_id: 1, adapter_id: 'hh' })
  expect(JSON.parse(String(createCall?.[1]?.body))).not.toHaveProperty('mode')
  expect(JSON.parse(String(createCall?.[1]?.body))).not.toHaveProperty('policy_id')
  expect(screen.queryByRole('button', { name: 'Продолжить' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Запустить' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Пауза' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Остановить' })).not.toBeInTheDocument()
})

test('paused session exposes only resume and stop actions', async () => {
  const pausedSession = {
    id: 12, profile_id: 1, adapter_id: 'hh', status: 'PAUSED',
    counters: { viewed: 2, filtered: 1 }, started_at: '2026-08-12T09:00:00Z', finished_at: null,
    stop_reason: 'Модель временно недоступна',
  }
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.endsWith('/api/sessions')) return Promise.resolve({ ok: true, json: async () => [pausedSession] })
    if (url.endsWith('/api/profiles')) return Promise.resolve({ ok: true, json: async () => [{ id: 1, data: { full_name: '', residence: '', job_search_locations: [], contacts: { phone: null, email: null, messengers: [] }, education: [], languages: [], driver_license: false }, created_at: '2026-08-13T00:00:00Z' }] })
    return Promise.resolve({ ok: true, json: async () => ({ connected: true, model_available: true, model: 'test' }) })
  }))

  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter initialEntries={['/session']}><App /></MemoryRouter></QueryClientProvider>)

  expect(await screen.findByRole('button', { name: 'Продолжить' })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Остановить' })).toBeInTheDocument()
  for (const name of ['Запустить', 'Пауза', 'Открыть браузер', 'Проверить вход', 'Повторить безопасный шаг']) {
    expect(screen.queryByRole('button', { name })).not.toBeInTheDocument()
  }
})

test('running session does not expose manual pause', async () => {
  const runningSession = { id: 44, profile_id: 1, adapter_id: 'hh', status: 'RUNNING', counters: {}, started_at: null, finished_at: null, stop_reason: null }
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.endsWith('/api/sessions')) return Promise.resolve({ ok: true, json: async () => [runningSession] })
    if (url.endsWith('/api/profiles')) return Promise.resolve({ ok: true, json: async () => [] })
    return Promise.resolve({ ok: true, json: async () => ({ connected: true, model_available: true, model: 'test' }) })
  }))
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter initialEntries={['/session']}><App /></MemoryRouter></QueryClientProvider>)
  expect(await screen.findByText(/СЕССИЯ #44/)).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Пауза' })).not.toBeInTheDocument()
})

test('displays legacy defaults while an older backend omits session limits', async () => {
  const legacySession = {
    id: 33, profile_id: 1, adapter_id: 'hh', status: 'PAUSED',
    counters: {}, started_at: null, finished_at: null, stop_reason: null,
  }
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.endsWith('/api/sessions')) return Promise.resolve({ ok: true, json: async () => [legacySession] })
    if (url.endsWith('/api/profiles')) return Promise.resolve({ ok: true, json: async () => [{ id: 1, data: { full_name: '', residence: '', job_search_locations: [], contacts: { phone: null, email: null, messengers: [] }, education: [], languages: [], driver_license: false }, created_at: '2026-08-13T00:00:00Z' }] })
    return Promise.resolve({ ok: true, json: async () => ({ connected: true, model_available: true, model: 'test' }) })
  }))
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter initialEntries={['/session']}><App /></MemoryRouter></QueryClientProvider>)
  expect(await screen.findByText(/Лимиты сессии: просмотр — 30; отправка — 5/)).toBeInTheDocument()
})

test('hides policy navigation, route UI, and API access', async () => {
  const fetchMock = vi.fn((input: RequestInfo | URL) => { void input; return Promise.resolve({ ok: true, json: async () => ({ connected: true, model_available: true, model: 'test' }) }) })
  vi.stubGlobal('fetch', fetchMock)
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter initialEntries={['/policy']}><App /></MemoryRouter></QueryClientProvider>)

  expect(screen.queryByRole('link', { name: 'Политика' })).not.toBeInTheDocument()
  expect(screen.queryByRole('textbox', { name: /Запрос для ИИ/ })).not.toBeInTheDocument()
  await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith('/api/policies'))).toBe(false))
})

test('shows resume relevance breakdown without policy UI', async () => {
  const vacancy = {
    id: 91,
    title: 'Product Manager',
    company: 'Example',
    url: 'https://example.test/vacancy/91',
    state: 'FILTERED_OUT',
    data: {},
    evaluation: {
      decision: 'skip', score: 44, confidence: 0.92, category: 'product', reason: 'Недостаточная релевантность по резюме',
      flag_filter: {
        reason: 'Найдена связь с продажами.',
        green_flags: [{ flag: 'Вакансия связана с SaaS', confidence: 0.86, evidence: ['B2B SaaS продукт'] }],
        red_flags: [{ flag: 'Вакансия связана с продажами', confidence: 0.91, evidence: ['план продаж'] }],
        work_format: { compatible: true },
      },
      score_breakdown: [
        { key: 'title', title: 'Название должности', description: '', max_points: 5, points: 4, explanation: 'Похоже', evidence: ['Product Manager'] },
        { key: 'green_flags', title: 'Green Flags', description: '', max_points: 20, points: 20, explanation: 'Скрыто', evidence: [] },
        { key: 'seniority', title: 'Уровень позиции', description: '', max_points: 0, points: 0, explanation: 'Не указан', evidence: [] },
      ],
    },
  }
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    if (String(input).endsWith('/api/vacancies')) return Promise.resolve({ ok: true, json: async () => [vacancy] })
    return Promise.resolve({ ok: true, json: async () => ({ connected: true, model_available: true, model: 'test' }) })
  }))
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter initialEntries={['/vacancies']}><App /></MemoryRouter></QueryClientProvider>)

  expect(await screen.findByText('Недостаточная релевантность по резюме')).toBeInTheDocument()
  expect(screen.getByText('РЕЛЕВАНТНОСТЬ ПО РЕЗЮМЕ')).toBeInTheDocument()
  expect(screen.getByText('Название должности')).toBeInTheDocument()
  expect(screen.queryByText('Уровень позиции')).not.toBeInTheDocument()
  expect(screen.getByText('Название должности')).toBeInTheDocument()
  expect(screen.getByText('5 / 5')).toBeInTheDocument()
  expect(screen.queryByText('Отклонено политическим фильтром')).not.toBeInTheDocument()
  expect(screen.queryByText('Принятые Green flags')).not.toBeInTheDocument()
  expect(screen.queryByText('Сработавшие Red flags')).not.toBeInTheDocument()
  expect(screen.queryByText('Сработал фильтр политики')).not.toBeInTheDocument()
})

test('converts legacy vacancy 2071 scores to weighted contributions', async () => {
  const scoreBreakdown = [
    ['title', 'Название должности', 2, 5],
    ['tasks', 'Задачи', 14, 30],
    ['industry', 'Сфера', 12, 25],
    ['required_years', 'Годы опыта', 14, 20],
    ['seniority', 'Уровень позиции', 0, 0],
    ['languages', 'Языки', 10, 10],
    ['skills', 'Навыки', 6, 10],
  ].map(([key, title, points, maxPoints]) => ({
    key, title, description: '', points, max_points: maxPoints, explanation: '', evidence: [],
  }))
  const vacancy = {
    id: 2071, title: 'Product Manager', company: 'Example', url: 'https://example.test/vacancy/2071',
    state: 'REJECTED_BY_MODEL', data: {},
    evaluation: { decision: 'skip', score: 52, confidence: 0.9, category: 'product', reason: 'Тест', score_breakdown: scoreBreakdown },
  }
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    if (String(input).endsWith('/api/vacancies')) return Promise.resolve({ ok: true, json: async () => [vacancy] })
    return Promise.resolve({ ok: true, json: async () => ({ connected: true, model_available: true, model: 'test' }) })
  }))
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter initialEntries={['/vacancies']}><App /></MemoryRouter></QueryClientProvider>)

  expect(await screen.findByText('Тест')).toBeInTheDocument()
  for (const value of ['3 / 5', '10 / 30', '13 / 25', '10 / 20', '10 / 10', '7 / 10']) {
    expect(screen.getByText(value)).toBeInTheDocument()
  }
  expect(screen.queryByText('Уровень позиции')).not.toBeInTheDocument()
})

test('shows six discrete relevance criteria and configured minimum failures', async () => {
  const scoreBreakdown = [
    ['title', 'Название должности', 2, 2],
    ['tasks', 'Задачи', 2, 3],
    ['industry', 'Сфера', 3, 4],
    ['required_years', 'Годы опыта', 1, 2],
    ['languages', 'Языки', 2, 2],
    ['skills', 'Навыки', 1, 3],
  ].map(([key, title, rawPoints, rawMaxPoints]) => ({
    key,
    title,
    description: '',
    points: 10,
    max_points: 20,
    raw_points: rawPoints,
    raw_max_points: rawMaxPoints,
    minimum_points: key === 'skills' ? 2 : null,
    minimum_failed: key === 'skills',
    explanation: `${title}: пояснение`,
    evidence: [],
  }))
  const vacancy = {
    id: 93,
    title: 'Product Manager',
    company: 'Example',
    url: 'https://example.test/vacancy/93',
    state: 'REJECTED_BY_MODEL',
    data: {},
    evaluation: {
      decision: 'skip', score: 76, confidence: 0.9, category: 'product', reason: 'Не пройден минимум по навыкам',
      score_breakdown: scoreBreakdown,
    },
  }
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    if (String(input).endsWith('/api/vacancies')) return Promise.resolve({ ok: true, json: async () => [vacancy] })
    return Promise.resolve({ ok: true, json: async () => ({ connected: true, model_available: true, model: 'test' }) })
  }))
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter initialEntries={['/vacancies']}><App /></MemoryRouter></QueryClientProvider>)

  expect(await screen.findByText('Не пройден минимум по навыкам')).toBeInTheDocument()
  for (const title of ['Название должности', 'Задачи', 'Сфера', 'Годы опыта', 'Языки', 'Навыки']) {
    expect(screen.getByText(title)).toBeInTheDocument()
  }
  expect(screen.getByText('5 / 5')).toBeInTheDocument()
  expect(screen.getByText('20 / 30')).toBeInTheDocument()
  expect(screen.getByText('19 / 25')).toBeInTheDocument()
  expect(screen.getByText('10 / 20')).toBeInTheDocument()
  expect(screen.getByText('10 / 10')).toBeInTheDocument()
  expect(screen.getByText('3 / 10')).toBeInTheDocument()
  expect(screen.getByText('Минимум: 2 — не выполнен')).toBeInTheDocument()
  expect(screen.getAllByText(/^Минимум:/)).toHaveLength(1)
})

test('does not show low-confidence red flag or work-format match as rejection', async () => {
  const vacancy = {
    id: 92,
    title: 'Product Manager',
    company: 'Example',
    url: 'https://example.test/vacancy/92',
    state: 'REJECTED_BY_MODEL',
    data: {},
    evaluation: {
      decision: 'skip', score: 60, confidence: 0.69, category: 'product', reason: 'Недостаточная релевантность',
      flag_filter: {
        reason: '',
        green_flags: [],
        red_flags: [{ flag: 'Вакансия связана с продажами', confidence: 0.69, matched: false, evidence: ['CRM'] }],
        work_format: { compatible: null, confidence: 0.69, vacancy_format: 'office', candidate_formats: ['remote'] },
      },
      score_breakdown: [],
    },
  }
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    if (String(input).endsWith('/api/vacancies')) return Promise.resolve({ ok: true, json: async () => [vacancy] })
    return Promise.resolve({ ok: true, json: async () => ({ connected: true, model_available: true, model: 'test' }) })
  }))
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter initialEntries={['/vacancies']}><App /></MemoryRouter></QueryClientProvider>)

  expect(await screen.findByText('Недостаточная релевантность')).toBeInTheDocument()
  expect(screen.queryByText('Отклонено политическим фильтром')).not.toBeInTheDocument()
  expect(screen.queryByText('Формат работы не подходит')).not.toBeInTheDocument()
  expect(screen.queryByText('Сработавшие Red flags')).not.toBeInTheDocument()
})
