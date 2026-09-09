import { afterEach, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { SessionQuestions } from './SessionQuestions';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const question = { id: 1, session_id: 3, question: 'Когда можете начать?', reason: 'Нет данных', options: [], context: {}, site: 'hh', vacancy_title: 'Разработчик', vacancy_url: 'https://hh.ru/vacancy/123', can_answer: true };

function mount({ fail = false, empty = false } = {}) {
  let pending = empty ? [] : [question];
  const requests: RequestInit[] = [];
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input).endsWith('/profiles')) return { ok: true, json: async () => [{ id: 1 }] };
    if (init?.method === 'POST') {
      requests.push(init);
      if (fail) return { ok: false, status: 503, json: async () => ({ detail: 'Сохранение недоступно' }) };
      pending = [];
      return { ok: true, json: async () => ({ ok: true }) };
    }
    return { ok: true, json: async () => pending };
  }));
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><SessionQuestions /></QueryClientProvider>);
  return requests;
}

test('asks after session and saves the user answer without showing a memory catalog', async () => {
  const requests = mount();
  expect(await screen.findByRole('dialog')).toHaveAccessibleName('Уточним несколько деталей');
  const vacancyLink = screen.getByRole('link', { name: 'Разработчик' });
  expect(vacancyLink).toHaveAttribute('href', question.vacancy_url);
  expect(vacancyLink).toHaveAttribute('target', '_blank');
  expect(vacancyLink).toHaveAttribute('rel', 'noopener noreferrer');
  const answer = screen.getByRole('textbox', { name: question.question });
  fireEvent.change(answer, { target: { value: ' Через две недели ' } });
  fireEvent.click(screen.getByRole('button', { name: 'Сохранить и продолжить' }));
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  expect(JSON.parse(String(requests[0].body))).toEqual({ answer: 'Через две недели', skip: false });
  expect(screen.queryByText('Через две недели')).not.toBeInTheDocument();
});

test('defers questions and allows reopening them without posting an answer', async () => {
  const requests = mount();
  await screen.findByRole('dialog');
  fireEvent.click(screen.getByRole('button', { name: 'Ответить позже' }));
  fireEvent.click(await screen.findByRole('button', { name: 'Ответить на вопросы (1)' }));
  expect(await screen.findByRole('dialog')).toBeInTheDocument();
  expect(requests).toHaveLength(0);
});

test('keeps the draft answer and shows save failures in the modal', async () => {
  mount({ fail: true });
  const answer = await screen.findByRole('textbox', { name: question.question });
  fireEvent.change(answer, { target: { value: 'Через две недели' } });
  fireEvent.click(screen.getByRole('button', { name: 'Сохранить и продолжить' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Сохранение недоступно');
  expect(answer).toHaveValue('Через две недели');
});

test('skip sends no answer and an empty queue never opens a dialog', async () => {
  const requests = mount();
  fireEvent.click(await screen.findByRole('button', { name: 'Пропустить вопрос' }));
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  expect(JSON.parse(String(requests[0].body))).toEqual({ answer: '', skip: true });
  cleanup();
  mount({ empty: true });
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
});
