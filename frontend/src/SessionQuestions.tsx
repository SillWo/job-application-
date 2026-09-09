import { useState } from "react";
import { Dialog } from "@base-ui/react/dialog";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./api";
import type { Profile } from "./types";
import "./session-questions.css";

type Question = {
  id: number; session_id: number; question: string; reason: string;
  options: string[]; context: Record<string, string>; site: string;
  vacancy_title: string | null; vacancy_url: string | null; can_answer: boolean;
};

function QuestionForm({ question, profileId, onSaved, answer, setAnswer }: { question: Question; profileId: number; onSaved: () => void; answer: string; setAnswer: (value: string) => void }) {
  const mutation = useMutation({
    mutationFn: (skip: boolean) => api(`/profiles/${profileId}/questions/${question.id}/answer`, {
      method: "POST", body: JSON.stringify({ answer: skip ? "" : answer.trim(), skip }),
    }),
    onSuccess: onSaved,
  });
  return <form onSubmit={(event) => { event.preventDefault(); mutation.mutate(false); }} className="session-question-form">
    <p className="question-origin">Сессия #{question.session_id} · {question.site}{question.vacancy_title && <> · {question.vacancy_url && /^https?:\/\//i.test(question.vacancy_url) ? <a href={question.vacancy_url} target="_blank" rel="noopener noreferrer">{question.vacancy_title}</a> : question.vacancy_title}</>}</p>
    {Object.values(question.context).length > 0 && <p className="question-context">{Object.values(question.context).join(" · ")}</p>}
    <label htmlFor="session-question-answer" className="question-label">{question.question}</label>
    {question.options.length > 0 && <p className="question-options">Варианты в анкете: {question.options.join(" · ")}</p>}
    {question.can_answer ? <textarea id="session-question-answer" value={answer} onChange={(event) => setAnswer(event.target.value)} rows={4} maxLength={4000} placeholder="Напишите свой ответ" autoComplete="off" /> : <p>Этот вопрос нельзя сохранить. Пропустите его и заполните на сайте самостоятельно.</p>}
    {mutation.isError && <p role="alert" className="question-error">{mutation.error instanceof Error ? mutation.error.message : "Не удалось сохранить ответ. Попробуйте снова."}</p>}
    <div className="question-actions">
      <button type="button" className="secondary" disabled={mutation.isPending} onClick={() => mutation.mutate(true)}>Пропустить вопрос</button>
      {question.can_answer && <button type="submit" className="primary" disabled={!answer.trim() || mutation.isPending}>{mutation.isPending ? "Сохраняю…" : "Сохранить и продолжить"}</button>}
    </div>
  </form>;
}

export function SessionQuestions() {
  const qc = useQueryClient();
  const profiles = useQuery({ queryKey: ["profiles"], queryFn: () => api<Profile[]>("/profiles") });
  const profileId = profiles.data?.[0]?.id;
  const questions = useQuery({
    queryKey: ["pending-questions", profileId],
    queryFn: async () => {
      const result = await api<Question[]>(`/profiles/${profileId}/pending-questions`);
      if (!Array.isArray(result)) throw new Error("Не удалось загрузить вопросы после сессии");
      return result;
    },
    enabled: Boolean(profileId), refetchInterval: 3000,
  });
  const [deferred, setDeferred] = useState<number[]>([]);
  const [handled, setHandled] = useState<number[]>([]);
  const [drafts, setDrafts] = useState<Record<number, string>>({});
  const pending = (questions.data ?? []).filter((question) => !handled.includes(question.id));
  const current = pending.find((question) => !deferred.includes(question.id));
  const defer = () => setDeferred(pending.map((question) => question.id));
  return <>
    {questions.isError && !current && <button type="button" className="secondary question-return" onClick={() => void questions.refetch()}>Повторить загрузку вопросов</button>}
    {!current && pending.length > 0 && <button type="button" className="secondary question-return" onClick={() => setDeferred([])}>Ответить на вопросы ({pending.length})</button>}
    <Dialog.Root open={Boolean(current)} onOpenChange={(open) => { if (!open) defer(); }}>
      <Dialog.Portal>
        <Dialog.Backdrop className="question-backdrop" />
        <Dialog.Popup className="question-dialog">
          <div className="question-heading"><span className="eyebrow">ПОСЛЕ СЕССИИ · ОСТАЛОСЬ {pending.length}</span><Dialog.Close className="secondary question-close" aria-label="Ответить позже">×</Dialog.Close></div>
          <Dialog.Title className="question-title">Уточним несколько деталей</Dialog.Title>
          <Dialog.Description className="question-description">ИИ не хватило информации для анкеты. Ваши ответы помогут ему в следующих сессиях на всех сайтах. Можно ответить позже или пропустить вопрос.</Dialog.Description>
          {current && profileId && <QuestionForm key={current.id} question={current} profileId={profileId} answer={drafts[current.id] ?? ""} setAnswer={(value) => setDrafts((values) => ({ ...values, [current.id]: value }))} onSaved={() => {
            setHandled((ids) => [...ids, current.id]);
            setDrafts((values) => { const next = { ...values }; delete next[current.id]; return next; });
            void qc.invalidateQueries({ queryKey: ["pending-questions", profileId] });
            void qc.invalidateQueries({ queryKey: ["notifications"] });
          }} />}
        </Dialog.Popup>
      </Dialog.Portal>
    </Dialog.Root>
  </>;
}
