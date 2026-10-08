SEARCH_INPUT_NAME = "text"
VACANCY_LINK = "a[data-qa='serp-item__title'], a[href*='/vacancy/']"
VACANCY_TITLE = "[data-qa='vacancy-title']"
COMPANY = "[data-qa='vacancy-company-name']"
DESCRIPTION = "[data-qa='vacancy-description']"
SALARY = "[data-qa='vacancy-salary'], [data-qa='vacancy-compensation']"
PAYMENT_FREQUENCY = "[data-qa='compensation-frequency-text']"
WORK_EXPERIENCE = "[data-qa='work-experience-text']"
EMPLOYMENT = "[data-qa='common-employment-text']"
HIRING_FORMAT = "[data-qa='vacancy-hiring-formats']"
WORK_SCHEDULE = "[data-qa='work-schedule-by-days-text']"
WORKING_HOURS = "[data-qa='working-hours-text']"
WORK_FORMAT = "[data-qa='work-formats-text']"
LOCATION = "[data-qa='vacancy-view-raw-address'], [data-qa='vacancy-address-with-map']"
RESPONSE_BUTTON = "[data-qa='vacancy-response-link-top'], [data-qa='vacancy-response-link-bottom']"
RESPONSE_SUBMIT = "[data-qa='vacancy-response-submit-popup']"
COVER_LETTER_TOGGLE = "[data-qa='vacancy-response-letter-toggle']"
COVER_LETTER_INPUT = "[data-qa='vacancy-response-popup-form-letter-input']"
ATTACH_COVER_LETTER = "[data-qa='responded-success-attach-cover-letter']"
COVER_LETTER_DIALOG = (
    "[role='dialog']:has([data-qa='vacancy-response-popup-form-letter-input'])"
    ":has([data-qa='vacancy-response-letter-submit'])"
    ":not(:has([data-qa='vacancy-response-submit-popup']))"
)
COVER_LETTER_SUBMIT = "[data-qa='vacancy-response-letter-submit']"
COVER_LETTER_BUSY = "[aria-busy='true'], [data-qa*='loading'], [data-qa*='spinner'], [class*='spinner']"
APPLICATION_QUESTION = "[data-qa^='vacancy-response-popup-form'] label"
APPLICATION_CONTROL = "textarea, select, input, [contenteditable='true'], [role='combobox']:not(select), [role='textbox']:not(input):not(textarea)"
# Limit controls to the actual response form; vacancy pages also have search,
# employer-question checkboxes and a footer language switcher.
APPLICATION_ROOT = "[data-qa='vacancy-response-popup-form'], [data-qa='vacancy-response-popup'], [role='dialog'], form:has([data-qa='vacancy-response-submit-popup'])"
TASK_CONTAINER = "xpath=ancestor::*[.//*[@data-qa='task-question']][1]"
FIELDSET = "xpath=ancestor::fieldset[1]"
WRAPPING_LABEL = "xpath=ancestor::label[1]"
LABEL = "label"
LEGEND = "legend"
OPTION = "option"
IS_SELECT = "xpath=self::select"
IS_TEXTAREA = "xpath=self::textarea"
IS_NATIVE_CONTROL = "xpath=self::input | self::textarea | self::select"
SELECTED_OPTIONS = "option:checked"
FOREIGN_NOTICE = "Вы откликаетесь на вакансию в другой стране"
FOREIGN_CONTINUE = "Все равно откликнуться"
FORM_ERROR = "[data-qa*='validation-error'], [data-qa='error-message'], [data-qa='vacancy-response-error'], [aria-invalid='true']"


def attribute_selector(tag: str, attribute: str, value: str) -> str:
    """Quote DOM identifiers as CSS strings; these never come from a model."""
    import json

    return f"{tag}[{attribute}={json.dumps(value, ensure_ascii=False)}]"
TASK_QUESTION = "[data-qa='task-question']"
ALREADY_APPLIED = "[data-qa='vacancy-response-link-view-topic']"
SUBMISSION_CONFIRMED = (
    "[data-qa='vacancy-response-success'], "
    "[data-qa='vacancy-response-popup-success']"
)
SUBMISSION_TEXT_MARKERS = (
    "вы откликнулись",
    "отклик отправлен",
    "отклик успешно отправлен",
    "ваш отклик отправлен",
)
# Only inspect visible challenge UI. Vacancy text can mention captchas as an
# ordinary topic, so generic body-text matching is intentionally avoided.
CAPTCHA_CHALLENGE = (
    "iframe[src*='captcha' i], iframe[title*='captcha' i], "
    "img[src*='captcha' i], img[alt*='captcha' i], "
    "input:not([type='hidden'])[name*='captcha' i], "
    "input:not([type='hidden'])[id*='captcha' i], "
    "input:not([type='hidden'])[placeholder*='captcha' i], "
    "[role='dialog']:has(img[alt*='captcha' i]):has(input:not([type='hidden'])), "
    "[data-qa*='captcha' i], [data-testid*='captcha' i], "
    "[data-smartcaptcha], [id*='captcha' i], [class*='captcha' i], "
    "[id*='smartcaptcha' i], [class*='smartcaptcha' i]"
)
CAPTCHA_DIALOG = "[role='dialog']"
CAPTCHA_TEXT_MARKERS = (
    "подтвердите, что вы не робот",
    "подтвердите, что вы человек",
    "пройдите проверку безопасности",
    "verify you are human",
    "security check",
)

# Explicit empty-result UI; a blank/error page is never exhaustion.
SEARCH_EMPTY = "[data-qa='vacancy-serp__vacancy-not-found'], [data-qa='vacancy-serp__no-results']"
SEARCH_PAGER = "[data-qa='pager-block']"
SEARCH_NEXT = "[data-qa='pager-next']"
DISCOVERY_LINKS = "a[href]"
RELATED_VACANCIES = "[data-qa*='similar'] a[href*='/vacancy/'], [data-qa*='related'] a[href*='/vacancy/'], [data-qa*='recommend'] a[href*='/vacancy/']"
