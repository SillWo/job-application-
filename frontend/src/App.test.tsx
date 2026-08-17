import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import App from './App'

beforeEach(() => { vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => [] })) })
afterEach(() => vi.unstubAllGlobals())

test('renders the Russian dashboard', async () => {
  render(<QueryClientProvider client={new QueryClient({defaultOptions:{queries:{retry:false}}})}><MemoryRouter><App /></MemoryRouter></QueryClientProvider>)
  expect(screen.getByText('Ваш поиск работы — под контролем')).toBeInTheDocument()
  expect(screen.getByText('Данные остаются локально')).toBeInTheDocument()
  expect(screen.getByText(/Ваш запрос на естественном языке учитывается/)).toBeInTheDocument()
  expect(screen.queryByText(/Сначала фильтры/)).not.toBeInTheDocument()
})

test('exposes every primary route through keyboard-accessible navigation', () => {
  render(<QueryClientProvider client={new QueryClient({defaultOptions:{queries:{retry:false}}})}><MemoryRouter><App /></MemoryRouter></QueryClientProvider>)
  const sidebar = screen.getByRole('complementary', { name: 'Основная навигация' })
  expect(sidebar).toHaveClass('sidebar')
  expect(sidebar.closest('.shell')?.querySelector(':scope > main')).toBeInTheDocument()
  const expected = [
    ['Обзор', '/'],
    ['Профиль', '/profile'],
    ['Сайты', '/sites'],
    ['Сессия', '/session'],
    ['Проверка', '/reviews'],
    ['Вакансии', '/vacancies'],
    ['Отчёты', '/reports'],
    ['Модель', '/model'],
  ]
  for (const [name, href] of expected) {
    expect(screen.getByRole('link', { name })).toHaveAttribute('href', href)
  }
})

test('renders report metrics and the PDF download link', async () => {
  const report = {
    id: 1,
    session_id: 9,
    created_at: '2026-08-12T03:14:46Z',
    pdf_url: '/api/reports/1/pdf',
    summary: {
      session_id: 9,
      started_at: null,
      finished_at: null,
      stop_reason: 'Лимит выдачи обработан',
      adapter: 'hh', mode: 'analysis_only', status: 'COMPLETED', counters: {}, vacancies: [],
      aggregates: { total: 30, evaluated: 30, matched: 20, submitted: 0, already_applied: 14, review: 0, errors: 6 },
    },
  }
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    if (String(input).endsWith('/api/reports')) return Promise.resolve({ ok: true, json: async () => [report] })
    return Promise.resolve({ ok: true, json: async () => ({ connected: true, model: 'test' }) })
  }))
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter initialEntries={['/reports']}><App /></MemoryRouter></QueryClientProvider>)
  expect(await screen.findByText('Сессия #9')).toBeInTheDocument()
  expect(screen.getByText('Лимит выдачи обработан')).toBeInTheDocument()
  const vacancyMetric = screen.getByText('Вакансий').closest('span')
  expect(vacancyMetric).not.toBeNull()
  expect(within(vacancyMetric as HTMLElement).getByText('30')).toBeInTheDocument()
  expect(screen.getByRole('link', { name: 'Скачать PDF' })).toHaveAttribute('href', '/api/reports/1/pdf')
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
  fireEvent.click(screen.getByRole('button', { name: '+ Мессенджер' }))
  expect(screen.getByLabelText('Мессенджер 1')).toBeInTheDocument()

  fireEvent.click(screen.getByRole('button', { name: 'Редактировать' }))
  expect(screen.getByRole('checkbox', { name: 'Удалённо' })).toBeChecked()
  fireEvent.click(screen.getByRole('checkbox', { name: 'Гибрид' }))
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
    mode: 'analysis_only',
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
  expect(JSON.parse(String(createCall?.[1]?.body))).not.toHaveProperty('policy_id')
  expect(screen.queryByRole('button', { name: 'Продолжить' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Запустить' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Пауза' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Остановить' })).not.toBeInTheDocument()
})

test('paused session exposes only resume and stop actions', async () => {
  const pausedSession = {
    id: 12, profile_id: 1, adapter_id: 'hh', mode: 'autopilot', status: 'PAUSED',
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

test('displays legacy defaults while an older backend omits session limits', async () => {
  const legacySession = {
    id: 33, profile_id: 1, adapter_id: 'hh', mode: 'autopilot', status: 'PAUSED',
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
  expect(screen.getByText('не применяется')).toBeInTheDocument()
  expect(screen.queryByText('Отклонено политическим фильтром')).not.toBeInTheDocument()
  expect(screen.queryByText('Принятые Green flags')).not.toBeInTheDocument()
  expect(screen.queryByText('Сработавшие Red flags')).not.toBeInTheDocument()
  expect(screen.queryByText('Сработал фильтр политики')).not.toBeInTheDocument()
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
