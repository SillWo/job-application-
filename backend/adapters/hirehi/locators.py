"""Shared, visible HireHi UI locators.

HireHi does not expose a public API contract.  Keep selectors in this module so
the adapter has one small place to update when the rendered UI changes.  The
first selector in each group is the stable hook observed in the live site;
the remaining selectors are deliberately conservative fallbacks for the
server-rendered/mobile variants.
"""

SEARCH_INPUT = "#searchInput, input[aria-label*='Компания'], input[placeholder*='Компания']"
LINKS = "a[href]"
DISCOVERY_LINKS = LINKS

# A card can be an article, a data-testid element, or a class-based tile.  We
# still fall back to anchors in discovery.py because an older fixture/site
# variant may render a flat list.
CARDS = (
    "article, [data-testid='vacancy-card'], [data-testid='job-card'], "
    "[class*='vacancy-card'], [class*='job-card']"
)
CARD_LINKS = "a[href]"
CARD_COMPANY = "[data-testid='vacancy-company'], [class*='company']"
CARD_TITLE = "h2, h3, [data-testid='vacancy-title'], [class*='title']"
CARD_SALARY = "[data-testid='vacancy-salary'], [class*='salary']"
CARD_GRADE = "[data-testid='vacancy-grade'], [class*='grade']"
CARD_LOCATION = "[data-testid='vacancy-location'], [class*='location']"
CARD_FORMAT = "[data-testid='vacancy-work-format'], [class*='format']"

# Vacancy detail fields are read from the visible detail/sidebar regions.  The
# sidebar scope matters because HireHi also renders a market-comparison salary
# widget whose number must not become the vacancy salary.
VACANCY_TITLE = "h1, [data-testid='vacancy-title'], .vacancy-title"
VACANCY_COMPANY = "a[href*='/companies/'], [data-testid='vacancy-company'], .vacancy-company"
VACANCY_DESCRIPTION = "[data-testid='vacancy-description'], .vacancy-description, [data-section='description']"
VACANCY_SIDEBAR = "aside, [data-testid='vacancy-sidebar'], .vacancy-sidebar, [class*='job-sidebar']"
VACANCY_SALARY = "[data-sidebar-field='salary'] .sidebar-value, [data-testid='vacancy-salary'], .vacancy-salary"
VACANCY_LOCATION = "[data-testid='vacancy-location'], .vacancy-location"
VACANCY_FORMAT = "[data-testid='vacancy-work-format'], .vacancy-work-format"
VACANCY_GRADE = "[data-testid='vacancy-grade'], .vacancy-grade"
# Skills belong to the primary vacancy section headed "навыки".  Keeping the
# section scope and job-tag class in the hook excludes related-vacancy cards,
# whose tags use the same generic ``job-tags`` wrapper.
VACANCY_SKILLS_SECTION = ".vacancy-section-content:has(.job-tags), [data-section='skills']"
VACANCY_SKILLS = "> .job-tags > a.job-tag.job-level[href*='search=%2B'], [data-testid='vacancy-skill']"

PAGINATION = "a[href*='page='], nav[aria-label*='страниц'] a, [role='navigation'] a"
NEXT_PAGE = (
    "a[rel='next'], a[aria-label*='След'], a[aria-label*='next'], "
    "a:has-text('Следующая'), a:has-text('Далее'), a:has-text('›'), a:has-text('→')"
)

# Similar vacancies are scoped to a visible section/card container.  This is
# important: the page footer and blog links must never become job references.
RELATED_VACANCIES = (
    "[data-testid='similar-vacancies'], [data-testid='related-vacancies'], "
    "section:has-text('Похожие вакансии'), [class*='similar-vacanc'], "
    "[class*='related-vacanc']"
)
PRO_FILTER = (
    "#matchMe, #matchMeCheckbox, [data-testid='match-me'], "
    "label:has-text('Подходят мне'), [role='checkbox'][aria-label*='Подходят мне']"
)

# Authentication is intentionally based on the protected profile shell.  The
# header's ``#btnLogin`` label is present for signed-in accounts too, so it is
# not a logout signal.  Keep these selectors structural and visible; generic
# words such as ``профиль`` or ``войти`` in page text are not reliable markers.
AUTH_PROFILE_MARKERS = (
    "#profileBlockDesktop, #profileAvatarDesktop, #profileEditDesktop, "
    "#profileMenuSubscriptionTierDesktop, a[href='/profile/cv'], "
    "a[href^='/profile/cv?'], a[href='/profile/applications'], "
    "a[href='/profile/settings'], [data-testid='profile-name'], "
    "[data-testid='profile-page']"
)

# A legacy server-rendered fixture used ``main h1`` as its only profile
# marker.  It is kept as a compatibility fallback in the adapter, but it is
# never considered outside the protected /profile route.
AUTH_PROFILE_LEGACY_MARKERS = (
    "main h1, main h2, [data-testid='profile-name'], [data-testid='profile-page']"
)

AUTH_LOGIN_FIELDS = (
    "input[type='password'], input[name='password'], "
    "input[autocomplete='current-password'], form[action*='login' i], "
    "[data-testid='login-form']"
)

# Only structural challenge widgets belong here.  In particular, do not add
# body-text selectors: job descriptions commonly mention Cloudflare,
# JavaScript challenges, or CAPTCHA as ordinary requirements.
CAPTCHA_CHALLENGE_MARKERS = (
    "iframe[src*='captcha' i], iframe[src*='recaptcha' i], "
    "iframe[src*='hcaptcha' i], iframe[src*='challenge' i], "
    "iframe[title*='captcha' i], iframe[title*='recaptcha' i], "
    "iframe[title*='hcaptcha' i], iframe[title*='challenge' i], "
    ".g-recaptcha, .h-captcha, #captcha, "
    "[data-testid*='captcha' i], [data-testid*='recaptcha' i], "
    "[data-testid*='challenge' i], [id*='smartcaptcha' i], "
    "[class*='smartcaptcha' i], [id*='captcha' i], [class*='captcha' i]"
)
