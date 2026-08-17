SEARCH_INPUT_NAME = "text"
VACANCY_LINK = "a[data-qa='serp-item__title'], a[href*='/vacancy/']"
VACANCY_TITLE = "[data-qa='vacancy-title']"
COMPANY = "[data-qa='vacancy-company-name']"
DESCRIPTION = "[data-qa='vacancy-description']"
RESPONSE_BUTTON = "[data-qa='vacancy-response-link-top'], [data-qa='vacancy-response-link-bottom']"
RESPONSE_SUBMIT = "[data-qa='vacancy-response-submit-popup']"
COVER_LETTER_TOGGLE = "[data-qa='vacancy-response-letter-toggle']"
COVER_LETTER_INPUT = "[data-qa='vacancy-response-popup-form-letter-input']"
APPLICATION_QUESTION = "[data-qa^='vacancy-response-popup-form'] label"
APPLICATION_CONTROL = "textarea, select, input"
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
CAPTCHA_MARKERS = ("captcha", "Подтвердите, что вы не робот")
