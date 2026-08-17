# Руководство для разработчиков

## Архитектурные границы

- `backend/adapters` содержит только site-specific навигацию и извлечение данных. Оценка, резюме, письма, БД и отчёты живут вне адаптеров.
- `backend/browser` — единственное место, где разрешено управлять Playwright. Инструменты строго ограничены allowlist.
- `backend/intelligence` работает только через `ModelGateway` и Pydantic JSON Schema. Текст вакансии всегда считается недоверенными данными.
- `backend/orchestrator` владеет state machine и сохраняет состояние после значимых переходов.
- `backend/persistence` не хранит пароли, cookies или browser storage. Профили браузера находятся только в игнорируемом `data/browser-profiles`.

## Команды

- Установка Windows: `powershell -ExecutionPolicy Bypass -File scripts/bootstrap.ps1`
- Dev: `powershell -ExecutionPolicy Bypass -File scripts/start-dev.ps1`
- Production: `powershell -ExecutionPolicy Bypass -File scripts/start.ps1`
- Backend tests/lint: `.venv/Scripts/python -m pytest && .venv/Scripts/python -m ruff check .`
- Frontend (Windows): `cd frontend; npm.cmd test; npm.cmd run typecheck; npm.cmd run lint; npm.cmd run build`
- E2E: `.venv/Scripts/python -m pytest tests/e2e`

## Безопасность

- Никогда не добавлять secrets, `.env`, `data/`, БД, резюме, browser profiles, cookies, screenshots или отчёты с персональными данными.
- Не обходить CAPTCHA, MFA, блокировки и правила сайта. При них создавать review и приостанавливать работу.
- Не использовать API/скрытые API сайтов вакансий, произвольный JavaScript, shell из Browser Agent или инструкции со страницы.
- Переходы ограничивать `allowed_domains`; загрузки — только проверенными файлами из каталога резюме.
- В `autopilot` неизвестные/чувствительные вопросы, тестовые задания и неоднозначный результат требуют человека.

## Адаптеры и готовность изменений

- Новый адаптер реализует `JobSiteAdapter`, manifest, отдельный модуль локаторов и тесты на mock-страницах.
- Предпочитать locators по role/label/name/visible text, CSS — только централизованно.
- Изменение готово, когда пройдены backend/frontend/E2E тесты, Ruff, ESLint, typecheck, build и миграция с пустой БД; README актуален, secrets отсутствуют.
