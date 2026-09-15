# Реестр решений по legacy-профилю

Документ фиксирует согласованные решения по legacy-данным. Это реестр
миграционных требований, а не описание выполненной очистки. Исторические
migrations переписывать нельзя.

## Зафиксированные агрегаты рабочей БД

- `candidate_profile`: 1 запись.
- `resume`: 1 запись.
- `profile_memory`: 13 записей.
- `session_questions`: 41 запись — 13 `answered`, 28 `skipped`, 0 `pending`.
- legacy-сессии: 47 записей, все terminal — 6 `completed`, 13 `failed`, 28 `stopped`.
- `applications` с `candidate_profile_id`: 697 записей.

Вакансии и отклики сохраняются. При удалении legacy-сессий `vacancy.session_id`
отвязывается (`NULL`), а сами вакансии и приложения не удаляются.

## Принятые решения

### A. Terminal legacy history — удалить

47 terminal legacy-сессий удаляются после проверки зависимостей. Перед удалением
сохраняются вакансии и отклики; ссылки вакансий отвязываются от сессий через
`vacancy.session_id = NULL`. Удаление не должно затрагивать nonterminal legacy
сессии: они являются migration blocker и требуют отдельного разбора, а не
тихого удаления.

Рекомендуемый порядок: сначала проверить внешние ключи и состав зависимостей,
затем удалить только подтверждённый terminal набор.

Решение пользователя: принято — удалить terminal legacy-сессии.

### B. `profile_memory` — удалить

13 записей legacy memory удаляются. Автоматический экспорт или миграция в
profileless/global memory не выполняются: это отдельный scope и отдельное
согласие.

Решение пользователя: принято — удалить `profile_memory`.

### C. Старый `candidate_profile`/`resume` content — удалить

Старое содержимое `candidate_profile` и `resume` удаляется после проверки
зависимостей и отвязки исторических ссылок. Его нельзя автоматически
превратить в site-specific saved resume source URL: для этого нужны публичный
URL конкретной площадки, проверка адаптером и явное подтверждение пользователя.

Вакансии и отклики остаются сохранёнными; удаление старого профиля не является
основанием удалять их. Nonterminal legacy-связи блокируют cleanup до отдельного
решения и не удаляются молча.

Решение пользователя: принято — удалить старый `candidate_profile`/`resume`.

### D. Уникальность `applications` — `unique(vacancy_id)`

Legacy-ограничение `unique(candidate_profile_id, vacancy_id)` заменяется на
`unique(vacancy_id)`. Существующие duplicate vacancy applications являются
migration blocker: их нужно инвентаризировать и разрешить явно, не удаляя
молча. До разрешения дублей constraint нельзя применять необратимо.

Решение пользователя: принято — целевая уникальность `unique(vacancy_id)`.

### E. Одна forward migration

После проверки blockers создаётся одна новая forward migration. Она выполняет
согласованный drop/null FK и cleanup:

- удаляет `candidate_profile`, `resume`, `profile_memory`;
- удаляет таблицы/колонки `session_answers` и `session_questions`;
- удаляет legacy `source_url_encrypted` из `saved_resume_sources` и
  `resume_preview_tokens`;
- удаляет legacy profile/session FK после отвязки вакансий и откликов;
- заменяет application uniqueness на `unique(vacancy_id)`.

Это удаление относится только к legacy-зашифрованной URL-обёртке. `private_view`
остаётся sealed: это содержимое резюме для локального рендеринга и legacy
fallback, а не публичная URL.

До запуска migration обязательны backup, проверка внешних ключей и проверка
пустой БД. Исторические migrations не переписываются. Nonterminal legacy
сессии и duplicate vacancy applications блокируют migration до явного разбора.

Решение пользователя: принято — выполнить cleanup одной forward migration.
