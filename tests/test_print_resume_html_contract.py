"""Contract checks for the bounded HH/Zarplata print DOM readers."""

import os
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from backend.adapters.hh.resume import extractor as hh_extractor
from backend.adapters.hh.resume import validate_resume_url as validate_hh
from backend.adapters.zarplata.resume import extractor as zarplata_extractor
from backend.adapters.zarplata.resume import validate_resume_url as validate_zarplata
from backend.services.resume_session import ResumeImportError, _normalize_extracted, public_preview

PRINT_HTML = """
<body class="bloko-print"><main>
  <div data-qa="resume-print-action"></div>
  <h2 data-qa="resume-personal-name">Sample Candidate</h2>
  <span data-qa="resume-personal-gender">Other</span>
  <span data-qa="resume-personal-age">31</span>
  <span data-qa="resume-personal-birthday">1 January 1995</span>
  <img data-qa="resume-photo" src="https://img.invalid/sample.png">
  <div data-qa="resume-block-contacts">
    <a data-qa="resume-contact-preferred" href="tel:+10000000001"><span data-qa="resume-contact-preferred-text">+1 000 000 0001</span></a>
    <span data-qa="resume-contact-phone">+1 000 000 0001</span>
    <div data-qa="resume-contact-email">sample@example.invalid</div>
    <a data-qa="resume-communication-method-telegram" href="https://t.me/sample"><span data-qa="resume-communication-method-telegram-text">Telegram</span></a>
    <a data-qa="resume-communication-method-setka" href="https://set.invalid/sample"><span data-qa="resume-communication-method-setka-text">Setka</span></a>
    <a data-qa="resume-phone-deep-link-viber">Viber</a>
    <a data-qa="resume-contact-link" href="https://sample.invalid/profile">Profile</a>
  </div>
  <div data-qa="resume-position">Product analyst</div>
  <div data-qa="resume-specialization-professional-role-value">Analytics, Product</div>
  <div data-qa="resume-specialization-employment-value">Full time, Contract</div>
  <div data-qa="resume-specialization-work-type-value">Remote, Hybrid</div>
  <span data-qa="resume-personal-address">Sample City</span>
  <span><span data-qa="relocation_relocation_possible">Country A, Country B, Country C</span></span>
  <div data-qa="resume-additional-info-business-trips-readiness">Ready</div>
  <div data-qa="resume-additional-info-citizenship">Country A, Country B</div>
  <div data-qa="resume-additional-info-work-ticket">Country A</div>
  <div data-qa="resume-additional-info-travel-time">Up to 45 minutes</div>
  <div data-qa="resume-about-content">A short summary with preserved content.</div>
  <section data-qa="resume-experience-block">
    <h2 data-qa="resume-experience-block-title">Experience: 4 years</h2>
    <article data-qa="resume-experience-item">
      <span data-qa="resume-experience-company-title">Example One</span>
      <span data-qa="resume-experience-company-area">Sample City</span>
      <a data-qa="resume-experience-company-url" href="https://example.invalid/one">one</a>
      <span data-qa="resume-experience-period-from">Jan 2022</span><span data-qa="resume-experience-period-to">Dec 2023</span>
      <span data-qa="resume-experience-value">2 years</span>
      <div data-qa="resume-experience-industry-title">Industry One</div><div data-qa="resume-experience-subindustry-title">Subindustry One</div>
      <div data-qa="resume-block-experience-position">Analyst</div><div data-qa="resume-block-experience-description">Did useful work.</div>
    </article>
    <article data-qa="resume-experience-item">
      <span data-qa="resume-experience-company-title">Example Two</span>
      <span data-qa="resume-experience-company-area">Other City</span>
      <span data-qa="resume-experience-period-from">Jan 2020</span><span data-qa="resume-experience-period-to">Dec 2021</span>
      <span data-qa="resume-experience-value">2 years</span>
      <div data-qa="resume-experience-industry-title">Industry Two</div>
      <div data-qa="resume-block-experience-position">Associate</div><div data-qa="resume-block-experience-description">Did other useful work.</div>
    </article>
  </section>
  <section data-qa="resume-education-block">
    <a data-qa="resume-education-item"><span data-qa="resume-education-institution">Example University</span><span data-qa="resume-education-faculty">Faculty of Science</span><span data-qa="resume-education-specialty">Data Science</span><span data-qa="resume-education-year">2020</span><span data-qa="resume-education-degree">Bachelor</span></a>
  </section>
  <section data-qa="resume-languages-block">
    <div data-qa="resume-language-item"><span data-qa="resume-language-name">English</span><span data-qa="resume-language-level">B2</span></div>
    <div data-qa="resume-language-item"><span data-qa="resume-language-name">Spanish</span><span data-qa="resume-language-level">A2</span></div>
  </section>
  <section data-qa="skills-table">
    <div data-qa="skill-level-title-3">Advanced</div><span data-qa="resume-skill"><span data-qa="resume-skill-name">Python</span><span data-qa="resume-skill-level">Strong</span></span><span data-qa="resume-skill">SQL</span>
  </section>
  <div data-qa="resume-block-contacts"><div data-qa="resume-view__text-with-overflow-tooltip">Telegram - @synthetic WhatsApp - +1 555 0100 ВК - https://vk.example/profile (@synthetic)</div></div>
  <div data-qa="resume-additional-info-self-employment">No</div>
  <div data-qa="job-search-status">Actively seeking work</div>
  <div data-qa="response-tag-achievements"><div data-qa="resume-card-tag-achievements">Concrete achievements</div></div>
  <section data-qa="resume-education-courses-block">
    <h2 data-qa="resume-education-courses-block-title">Professional development courses</h2>
    <a data-qa="cell"><div data-qa="cell-left-side">
      <div class="magritte-text_style-primary"><span>Synthetic Flutter development</span></div>
      <div class="magritte-text_style-secondary"><span>Synthetic Federal University, Programmer</span><span> • </span>2023</div>
    </div></a>
  </section>
</main></body>
"""


async def _offline_page(playwright):
    browser = await playwright.chromium.launch(headless=True)
    context = await browser.new_context(java_script_enabled=False)

    async def abort(route):
        # Fixtures are injected with set_content or loaded from local files.
        # Never continue arbitrary document requests: an iframe/meta refresh
        # must not turn this deterministic contract test into a network test.
        if route.request.resource_type == "document" and route.request.url.startswith("file:///"):
            await route.continue_()
        else:
            await route.abort()

    await context.route("**/*", abort)
    return browser, context, await context.new_page()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("extractor", "url", "site"),
    [
        (hh_extractor, "https://hh.ru/resume/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "hh"),
        (zarplata_extractor, "https://zarplata.ru/resume/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "zarplata"),
    ],
)
async def test_print_contract_maps_structured_variable_sections(extractor, url, site):
    async with async_playwright() as playwright:
        browser, context, page = await _offline_page(playwright)
        await page.set_content(PRINT_HTML)
        ref = (validate_hh if site == "hh" else validate_zarplata)(url)
        snapshots = [await extractor.extract(page, ref, extractor.policy) for _ in range(10)]
        snapshot = snapshots[0]
        await browser.close()

    assert {item.content_hash for item in snapshots} == {snapshot.content_hash}
    assert snapshot.schema_version >= 2
    assert snapshot.extractor_version.startswith(f"{site}-print-")
    assert snapshot.contacts.email.value == "sample@example.invalid"
    assert snapshot.contacts.contact_comment.value.startswith("Telegram - @synthetic")
    assert snapshot.self_employment.value == "No"
    assert snapshot.job_search_status.value == "Actively seeking work"
    assert snapshot.source_badges.value == ["Concrete achievements"]
    assert snapshot.courses[0].name.value == "Synthetic Flutter development"
    assert snapshot.courses[0].institution.value == "Synthetic Federal University"
    assert snapshot.courses[0].description.value == "Programmer"
    assert snapshot.courses[0].year.value == "2023"
    assert snapshot.identity.birth_date.value == "1 January 1995"
    assert snapshot.contacts.messengers.value == ["https://t.me/sample", "https://set.invalid/sample"]
    assert snapshot.target.employment_types.value == ["Full time", "Contract"]
    assert snapshot.location.relocation.value == "Country A, Country B, Country C"
    assert len(snapshot.experience) == 2
    assert snapshot.experience[0].company.value == "Example One"
    assert snapshot.experience[1].company.value == "Example Two"
    assert snapshot.experience[0].company_url.value == "https://example.invalid/one"
    assert snapshot.experience[1].company_url.availability.value == "not_provided"
    assert len(snapshot.education) == 1
    assert len(snapshot.languages) == 2
    assert [item.name.value for item in snapshot.skills] == ["Python", "SQL"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("extractor", "validator", "url"),
    [
        (hh_extractor, validate_hh, "https://hh.ru/resume/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
        (zarplata_extractor, validate_zarplata, "https://zarplata.ru/resume/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"),
    ],
)
async def test_print_contract_normalizes_explicit_russian_gender_and_keeps_provenance(
    extractor, validator, url
):
    async with async_playwright() as playwright:
        browser, context, page = await _offline_page(playwright)
        try:
            ref = validator(url)
            for label, expected in (
                ("мУЖЧиНа", "male"), ("  ЖЕНЩИНА  ", "female"),
                ("male", "male"), ("female", "female"), ("Other", "Other"),
            ):
                html = PRINT_HTML.replace(
                    '<span data-qa="resume-personal-gender">Other</span>',
                    f'<span data-qa="resume-personal-gender">{label}</span>',
                )
                await page.set_content(html)
                snapshot = await extractor.extract(page, ref, extractor.policy)
                assert snapshot.identity.gender.value == expected
                assert snapshot.identity.gender.availability.value == "present"
                assert snapshot.identity.gender.source_section == "identity"
                preview = public_preview(snapshot)
                if expected in {"male", "female"}:
                    assert preview["grammatical_gender"] == expected
                    assert preview["questions"] == []
                else:
                    assert preview["grammatical_gender"] is None
                    assert len(preview["questions"]) == 1

            hidden_html = PRINT_HTML.replace(
                '<span data-qa="resume-personal-gender">Other</span>',
                '<span data-qa="resume-personal-gender" style="display:none">Мужчина</span>',
            )
            await page.set_content(hidden_html)
            hidden = await extractor.extract(page, ref, extractor.policy)
            assert hidden.identity.gender.value is None
            assert hidden.identity.gender.availability.value == "hidden"

            missing_html = PRINT_HTML.replace(
                '<span data-qa="resume-personal-gender">Other</span>', ""
            )
            await page.set_content(missing_html)
            missing = await extractor.extract(page, ref, extractor.policy)
            assert missing.identity.gender.value is None
            assert missing.identity.gender.availability.value == "not_provided"
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("extractor", "validator", "url"),
    [
        (hh_extractor, validate_hh, "https://hh.ru/resume/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
        (zarplata_extractor, validate_zarplata, "https://zarplata.ru/resume/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"),
    ],
)
async def test_print_contract_reads_native_language_heading_only_for_following_row(extractor, validator, url):
    html = PRINT_HTML.replace(
        '<section data-qa="resume-languages-block">\n'
        '    <div data-qa="resume-language-item"><span data-qa="resume-language-name">English</span><span data-qa="resume-language-level">B2</span></div>\n'
        '    <div data-qa="resume-language-item"><span data-qa="resume-language-name">Spanish</span><span data-qa="resume-language-level">A2</span></div>\n'
        '  </section>',
        '<section data-qa="resume-languages-block">\n'
        '    <div class="magritte-v-spacing-container"><div class="title--native"><span class="magritte-text_style-primary">Родной</span></div>'
        '<div data-qa="resume-language-item"><span data-qa="resume-language-name">Русский</span></div></div>\n'
        '    <div class="magritte-v-spacing-container"><div class="title--other">Другие языки</div>'
        '<div data-qa="resume-language-item"><span data-qa="resume-language-name">English</span><span data-qa="resume-language-level">B1</span></div>'
        '<div data-qa="resume-language-item"><span data-qa="resume-language-name">Spanish</span></div></div>\n'
        '    <div class="magritte-v-spacing-container"><div class="title--hidden"><span class="magritte-text_style-primary" style="display:none">Родной</span></div>'
        '<div data-qa="resume-language-item"><span data-qa="resume-language-name">French</span></div></div>\n'
        '  </section>',
    )
    async with async_playwright() as playwright:
        browser, context, page = await _offline_page(playwright)
        await page.set_content(html)
        snapshot = await extractor.extract(page, validator(url), extractor.policy)
        await browser.close()

    assert [(item.language.value, item.proficiency.value) for item in snapshot.languages] == [
        ("Русский", "Родной"), ("English", "B1"), ("Spanish", None), ("French", None),
    ]
    assert snapshot.languages[3].proficiency.availability.value == "not_provided"


@pytest.mark.asyncio
async def test_print_contract_mutation_changes_hash_and_count_without_duplication():
    async with async_playwright() as playwright:
        browser, context, page = await _offline_page(playwright)
        await page.set_content(PRINT_HTML)
        ref = validate_hh("https://hh.ru/resume/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        first = await hh_extractor.extract(page, ref, hh_extractor.policy)
        await page.set_content(PRINT_HTML.replace("A short summary with preserved content.", "A changed summary."))
        changed = await hh_extractor.extract(page, ref, hh_extractor.policy)
        expanded_html = PRINT_HTML.replace(
            "</section>\n  <section data-qa=\"resume-education-block\">",
            "<article data-qa=\"resume-experience-item\"><span data-qa=\"resume-experience-company-title\">Example Three</span><span data-qa=\"resume-experience-period-from\">Jan 2018</span><span data-qa=\"resume-experience-period-to\">Dec 2019</span><span data-qa=\"resume-experience-value\">2 years</span><div data-qa=\"resume-block-experience-position\">Assistant</div></article></section>\n  <section data-qa=\"resume-education-block\">",
        )
        await page.set_content(expanded_html)
        expanded = await hh_extractor.extract(page, ref, hh_extractor.policy)
        await browser.close()
    assert first.content_hash != changed.content_hash
    assert len(expanded.experience) == 3


@pytest.mark.asyncio
async def test_print_contract_preserves_hidden_field_and_rejects_damaged_experience():
    async with async_playwright() as playwright:
        browser, context, page = await _offline_page(playwright)
        hidden = PRINT_HTML.replace(
            '<div data-qa="resume-contact-email">sample@example.invalid</div>',
            '<div data-qa="resume-contact-email" style="display:none">sample@example.invalid</div>',
        )
        await page.set_content(hidden)
        ref = validate_hh("https://hh.ru/resume/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        snapshot = await hh_extractor.extract(page, ref, hh_extractor.policy)
        damaged = PRINT_HTML.replace('<span data-qa="resume-experience-company-title">Example One</span>', "")
        await page.set_content(damaged)
        with pytest.raises(ValueError, match="experience"):
            await hh_extractor.extract(page, ref, hh_extractor.policy)
        await browser.close()
    assert snapshot.contacts.email.availability.value == "hidden"


@pytest.mark.asyncio
async def test_opt_in_local_print_html_is_offline_and_deterministic():
    root = os.environ.get("LOCAL_RESUME_HTML_DIR") or os.environ.get("LOCAL_HTML_DIR")
    if not root:
        pytest.skip("LOCAL_HTML_DIR is not set")
    async with async_playwright() as playwright:
        browser, context, page = await _offline_page(playwright)
        for filename, extractor, validator, url in (
            ("HH.html", hh_extractor, validate_hh, "https://hh.ru/resume/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
            ("zarplata.html", zarplata_extractor, validate_zarplata, "https://zarplata.ru/resume/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"),
        ):
            path = Path(root, filename)
            assert path.is_file(), f"required local resume fixture is missing: {path}"
            await page.goto(path.resolve().as_uri(), wait_until="domcontentloaded")
            ref = validator(url)
            snapshots = [await extractor.extract(page, ref, extractor.policy) for _ in range(10)]
            assert len({item.content_hash for item in snapshots}) == 1
            sample = snapshots[0]
            assert sample.education[0].specialty.value == "Информационная безопасность"
            assert sample.education[0].degree.value == "Высшее образование (Бакалавр)"
            assert sample.education[0].end_date.value == "2026"
            if filename == "HH.html":
                assert "Узбекистан" in (sample.location.relocation.value or "")
            else:
                assert sample.location.relocation.value == "готов к переезду"
            assert sample.source_updated_text.value in {"17.09.2026", "13.09.2026"}
        await browser.close()


@pytest.mark.asyncio
async def test_extract_rejects_non_print_dom_without_owner_fallback():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content('<div data-qa="resume-personal-name">Candidate</div><div data-qa="resume-position">Role</div>')
        ref = validate_hh("https://hh.ru/resume/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        with pytest.raises(ValueError, match="print"):
            await hh_extractor.extract(page, ref, hh_extractor.policy)
        await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("extractor", "validator", "url", "site"),
    [
        (hh_extractor, validate_hh, "https://hh.ru/resume/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "HH"),
        (zarplata_extractor, validate_zarplata, "https://zarplata.ru/resume/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "Zarplata"),
    ],
)
async def test_print_contract_keeps_hidden_partial_records_and_ignores_service_duplicates(
    extractor, validator, url, site,
):
    ref = validator(url)
    mutations = (
        (
            "languages",
            PRINT_HTML.replace(
                '<div data-qa="resume-language-item"><span data-qa="resume-language-name">Spanish</span><span data-qa="resume-language-level">A2</span></div>',
                '<div data-qa="resume-language-item" style="display:none"><span data-qa="resume-language-name">Spanish</span><span data-qa="resume-language-level">A2</span></div>',
            ).replace(
                '<section data-qa="resume-languages-block">',
                '<nav data-qa="resume-service-navigation" style="display:none"><div data-qa="resume-language-item">Navigation duplicate</div></nav><section data-qa="resume-languages-block">',
            ),
            "language",
        ),
        (
            "skills",
            PRINT_HTML.replace(
                '<span data-qa="resume-skill">SQL</span>',
                '<span data-qa="resume-skill" style="display:none">SQL</span>',
            ),
            "skill",
        ),
        (
            "experience",
            PRINT_HTML.replace(
                '<article data-qa="resume-experience-item">\n      <span data-qa="resume-experience-company-title">Example Two</span>',
                '<article data-qa="resume-experience-item" style="display:none">\n      <span data-qa="resume-experience-company-title">Example Two</span>',
            ),
            "experience",
        ),
    )
    async with async_playwright() as playwright:
        browser, context, page = await _offline_page(playwright)
        try:
            for _section, html, kind in mutations:
                await page.set_content(html)
                snapshots = [await extractor.extract(page, ref, extractor.policy) for _ in range(10)]
                assert {item.content_hash for item in snapshots} == {snapshots[0].content_hash}, site
                snapshot = snapshots[0]
                if kind == "language":
                    assert len(snapshot.languages) == 2
                    assert snapshot.languages[0].language.value == "English"
                    assert snapshot.languages[1].language.value is None
                    assert "languages[1].language" in snapshot.coverage.hidden_fields
                    assert "languages[2]" not in " ".join(snapshot.coverage.hidden_fields)
                elif kind == "skill":
                    assert [item.name.value for item in snapshot.skills[:1]] == ["Python"]
                    assert len(snapshot.skills) == 2
                    assert snapshot.skills[1].name.value is None
                    assert "skills[1].name" in snapshot.coverage.hidden_fields
                else:
                    assert len(snapshot.experience) == 2
                    assert snapshot.experience[0].company.value == "Example One"
                    assert snapshot.experience[1].company.value is None
                    assert "experience[1].company" in snapshot.coverage.hidden_fields
        finally:
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("extractor", "validator", "url"),
    [
        (hh_extractor, validate_hh, "https://hh.ru/resume/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
        (zarplata_extractor, validate_zarplata, "https://zarplata.ru/resume/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"),
    ],
)
async def test_print_contract_rejects_unknown_content_section(extractor, validator, url):
    html = PRINT_HTML.replace(
        "</main>",
        '<section data-qa="resume-new-achievements-block"><h2>Achievements</h2><p>Delivered measurable results.</p></section></main>',
    )
    async with async_playwright() as playwright:
        browser, context, page = await _offline_page(playwright)
        try:
            await page.set_content(html)
            with pytest.raises(ValueError, match=r"unknown content section parse error"):
                await extractor.extract(page, validator(url), extractor.policy)
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_resume_service_rejects_snapshot_with_hidden_coverage():
    async with async_playwright() as playwright:
        browser, context, page = await _offline_page(playwright)
        try:
            html = PRINT_HTML.replace(
                '<div data-qa="resume-language-item"><span data-qa="resume-language-name">Spanish</span><span data-qa="resume-language-level">A2</span></div>',
                '<div data-qa="resume-language-item" style="display:none"><span data-qa="resume-language-name">Spanish</span><span data-qa="resume-language-level">A2</span></div>',
            )
            await page.set_content(html)
            ref = validate_hh("https://hh.ru/resume/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
            snapshot = await hh_extractor.extract(page, ref, hh_extractor.policy)
        finally:
            await browser.close()
    with pytest.raises(ResumeImportError):
        _normalize_extracted(
            snapshot,
            adapter_id="hh",
            source_url=ref.url,
            source_ref=ref,
        )
