"""Sanitized DOM contract tests for HireHi resume and vacancy extraction."""

import os
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from backend.adapters.base.resume_import import snapshot_hash
from backend.adapters.hirehi.adapter import HireHiAdapter
from backend.adapters.hirehi.resume import POLICY, extractor

RESUME_HTML = """
<main>
  <section class="resume-public-card resume-public-header-card">
    <div class="resume-public-header">
      <div class="resume-public-name">Sample Candidate</div>
      <div class="resume-public-position">Product Manager</div>
      <div class="resume-public-meta">Senior · Full-time · Remote, Office</div>
      <a class="resume-public-phone" href="tel:+70000000000">phone</a>
      <a class="resume-public-email-link" href="mailto:sample@example.test">email</a>
      <a class="resume-public-contact-link" href="https://t.me/sample">telegram</a>
      <div class="resume-public-location">Sample City</div>
      <div class="resume-public-about">A short sanitized profile.</div>
    </div>
  </section>
  <section class="resume-public-card"><h2 class="resume-public-section-title">Experience · 4 years</h2><div class="resume-public-list">
    <div class="resume-public-list-item"><div class="resume-public-list-dates"><span class="resume-public-date-line">Jan 2020 —</span><span class="resume-public-date-line">Jan 2021</span></div><div class="resume-public-list-title">Acme</div><span class="resume-public-list-company">Acme</span><div class="resume-public-list-subtitle">Product Manager · Sample City</div><span class="resume-public-location-meta">Sample City</span><span class="resume-public-work-format">Remote</span><span class="resume-public-employment-type">Full-time</span><span class="resume-public-grade">Senior</span><div class="resume-public-duration">1 year</div><div class="resume-public-list-description"><p>Duty one.</p><div class="resume-public-achievements-list"><div class="resume-public-achievements-item">Achievement one.</div></div></div></div>
    <div class="resume-public-list-item"><div class="resume-public-list-title">Beta</div><span class="resume-public-list-company">Beta</span><div class="resume-public-list-subtitle">Project Manager</div><div class="resume-public-list-description"><p>Duty two.</p></div></div>
    <div class="resume-public-list-item"><div class="resume-public-list-title">Gamma</div><span class="resume-public-list-company">Gamma</span><div class="resume-public-list-subtitle">Analyst</div><div class="resume-public-list-description"><p>Duty three.</p></div></div>
  </div></section>
  <section class="resume-public-card"><h2 class="resume-public-section-title">Education</h2><div class="resume-public-list"><div class="resume-public-list-item"><span class="resume-public-list-company">Sample University</span><div class="resume-public-list-title">Sample University</div><div class="resume-public-list-subtitle">Bachelor · Computer Science</div><span class="resume-public-date-line">Sep 2016 — Jun 2020</span></div></div></section>
  <section class="resume-public-card"><h2 class="resume-public-section-title">Skills</h2><div class="resume-public-skills-row"><div class="resume-public-skills-label">Tools</div><div><span class="resume-public-skill-chip">Python</span><span class="resume-public-skill-chip">SQL</span></div></div><div class="resume-public-language-line"><span class="resume-public-language-name">English</span><span class="resume-public-language-level">Advanced</span></div></section>
  <section class="resume-public-card"><h2 class="resume-public-section-title">Awards</h2><div class="resume-public-list"><div class="resume-public-list-item"><span class="resume-public-list-company">Sample Grant</span><div class="resume-public-list-title">Sample Grant</div><span class="resume-public-date-line">2022</span></div></div></section>
  <section class="resume-public-card"><h2 class="resume-public-section-title">Courses</h2><div class="resume-public-list"><div class="resume-public-list-item"><span class="resume-public-list-company">Sample Course</span><div class="resume-public-list-title">Sample Course</div><span class="resume-public-date-line">2021</span></div></div></section>
  <section class="resume-public-card"><h2 class="resume-public-section-title">Volunteer notes</h2><div class="resume-public-list-item">Unknown section content.</div></section>
</main>
"""


async def _isolated_page(playwright, html: str):
    browser = await playwright.chromium.launch()
    context = await browser.new_context(java_script_enabled=False)

    async def abort(route):
        await route.abort()

    await context.route("**/*", abort)
    page = await context.new_page()
    await page.set_content(html, wait_until="domcontentloaded")
    return browser, context, page


@pytest.mark.asyncio
async def test_hirehi_resume_sections_are_scoped_and_links_preserved():
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser, context, page = await _isolated_page(playwright, RESUME_HTML)
        snapshot = await extractor.extract(page, POLICY.validate("https://hirehi.ru/resume/Sample_1"), POLICY)
        await context.close()
        await browser.close()

    assert len(snapshot.experience) == 3
    assert len(snapshot.education) == 1
    assert len(snapshot.awards) == 1
    assert len(snapshot.courses) == 1
    assert snapshot.experience[0].position.value == "Product Manager"
    assert snapshot.experience[0].duties.value == "Duty one."
    assert snapshot.experience[0].achievements.value == ["Achievement one."]
    assert snapshot.contacts.phone.value == "+70000000000"
    assert snapshot.contacts.email.value == "sample@example.test"
    assert snapshot.contacts.messengers.value == ["https://t.me/sample"]
    assert snapshot.target.work_formats.value == ["Remote", "Office"]
    assert snapshot.additional_sections[0].name == "Volunteer notes"
    assert "Unknown section content." in snapshot.additional_sections[0].content.value


@pytest.mark.asyncio
async def test_hirehi_language_level_container_does_not_duplicate_nested_description():
    html = """
      <main>
        <section class="resume-public-card">
          <h2 class="resume-public-section-title">Languages</h2>
          <div class="resume-public-language-line">
            <span class="resume-public-language-name">English</span>
            <span class="resume-public-language-level">B1 — <span class="resume-public-language-desc">Средний уровень</span></span>
          </div>
          <div class="resume-public-language-line">
            <span class="resume-public-language-name">Русский</span>
            <span class="resume-public-language-level">Носитель — <span class="resume-public-language-desc">Родной язык</span></span>
          </div>
          <div class="resume-public-language-line">
            <span class="resume-public-language-name">Deutsch</span>
            <span data-field="proficiency">A2</span>
          </div>
        </section>
      </main>
    """
    async with async_playwright() as playwright:
        browser, context, page = await _isolated_page(playwright, html)
        snapshot = await extractor.extract(
            page, POLICY.validate("https://hirehi.ru/resume/Sample_1"), POLICY
        )
        await context.close()
        await browser.close()

    assert [language.proficiency.value for language in snapshot.languages] == [
        "B1 — Средний уровень", "Носитель — Родной язык", "A2"
    ]


@pytest.mark.asyncio
async def test_hirehi_resume_import_is_repeatable_and_hashes_content_only():
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser, context, page = await _isolated_page(playwright, RESUME_HTML)
        snapshots = [await extractor.extract(page, POLICY.validate("https://hirehi.ru/resume/Sample_1"), POLICY) for _ in range(10)]
        await context.close()
        await browser.close()

    dumps = [snapshot.model_dump(mode="json", exclude={"imported_at"}) for snapshot in snapshots]
    assert all(dump == dumps[0] for dump in dumps)
    assert len({snapshot.content_hash for snapshot in snapshots}) == 1
    assert snapshots[0].content_hash == snapshot_hash(snapshots[0])


@pytest.mark.asyncio
async def test_hirehi_resume_mutation_changes_the_corresponding_record_and_hash():
    from playwright.async_api import async_playwright

    mutated = RESUME_HTML.replace("Product Manager · Sample City", "Changed Role · Sample City", 1)
    async with async_playwright() as playwright:
        browser, context, page = await _isolated_page(playwright, RESUME_HTML)
        original = await extractor.extract(page, POLICY.validate("https://hirehi.ru/resume/Sample_1"), POLICY)
        await page.set_content(mutated, wait_until="domcontentloaded")
        changed = await extractor.extract(page, POLICY.validate("https://hirehi.ru/resume/Sample_1"), POLICY)
        await context.close()
        await browser.close()

    assert original.experience[0].position.value == "Product Manager"
    assert changed.experience[0].position.value == "Changed Role"
    assert changed.content_hash != original.content_hash


@pytest.mark.asyncio
async def test_hirehi_vacancy_reads_sidebar_salary_and_skips_market_comparison():
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser, context, page = await _isolated_page(playwright, """
          <main><h1>Visible role</h1><a data-testid="vacancy-company">Acme</a>
            <div data-testid="vacancy-description">Full visible description.</div>
            <aside data-testid="vacancy-sidebar">
              <div data-testid="vacancy-salary">100 000 – 150 000 ₽</div>
              <div class="salary market-comparison">Average market 80 000 ₽</div>
              <div data-testid="vacancy-location">Sample City</div>
              <div data-testid="vacancy-work-format">Remote</div>
              <div data-testid="vacancy-grade">Senior</div>
              <div data-testid="vacancy-skill">Python</div>
            </aside>
          </main>
        """)
        job = await HireHiAdapter().extract_job(page)
        await context.close()
        await browser.close()

    assert job.description == "Full visible description."
    assert job.salary and job.salary.minimum == 100000 and job.salary.maximum == 150000
    assert job.location == "Sample City"
    assert job.work_format == "Remote"
    assert job.grade == "Senior"
    assert job.required_skills == ["Python"]


@pytest.mark.asyncio
async def test_hirehi_live_sidebar_and_primary_skills_shape_without_testids():
    skills = "".join(
        f'<a class="job-tag job-level" href="/search=%2Bskill-{index}">Skill {index}</a>'
        for index in range(14)
    )
    html = f"""
      <main><h1>Industrial AI Engineer</h1>
        <div data-testid="vacancy-description">Synthetic vacancy description.</div>
        <div class="vacancy-sidebar">
          <div class="sidebar-item" data-sidebar-field="salary">
            <div class="sidebar-label">&#1079;&#1072;&#1088;&#1087;&#1083;&#1072;&#1090;&#1072;</div>
            <span class="sidebar-value">&#1086;&#1090; 250 000 &#8381;</span>
          </div>
          <div class="sidebar-item"><span class="sidebar-label">&#1075;&#1088;&#1077;&#1081;&#1076;</span><a class="sidebar-value">senior</a></div>
          <div class="sidebar-item"><span class="sidebar-label">&#1092;&#1086;&#1088;&#1084;&#1072;&#1090;</span><a class="sidebar-value">&#1075;&#1080;&#1073;&#1088;&#1080;&#1076; &#1052;&#1086;&#1089;&#1082;&#1074;&#1072;</a></div>
          <div class="sidebar-item"><span class="sidebar-label">&#1089;&#1090;&#1088;&#1072;&#1085;&#1072;</span><span class="sidebar-value">&#1056;&#1086;&#1089;&#1089;&#1080;&#1103;</span></div>
        </div>
        <section class="vacancy-section-content"><h2>&#1085;&#1072;&#1074;&#1099;&#1082;&#1080;</h2><div class="job-tags">{skills}</div></section>
        <section class="similar-vacancies"><div class="job-tags"><div class="job-tag job-level">senior</div><div class="job-tag job-level">hybrid</div></div></section>
      </main>
    """
    mutated = html.replace(
        '<div class="sidebar-item" data-sidebar-field="salary">\n'
        '            <div class="sidebar-label">&#1079;&#1072;&#1088;&#1087;&#1083;&#1072;&#1090;&#1072;</div>\n'
        '            <span class="sidebar-value">&#1086;&#1090; 250 000 &#8381;</span>\n'
        '          </div>',
        "",
    ).replace("</main>", '<div class="salary market-comparison">293 851 ₽</div></main>')
    async with async_playwright() as playwright:
        browser, context, page = await _isolated_page(playwright, html)
        job = await HireHiAdapter().extract_job(page)
        await page.set_content(mutated, wait_until="domcontentloaded")
        changed = await HireHiAdapter().extract_job(page)
        await context.close()
        await browser.close()

    assert job.salary and job.salary.minimum == 250000 and job.salary.maximum is None
    assert job.grade == "senior"
    assert job.work_format == "гибрид"
    assert job.location == "Россия, Москва"
    assert job.required_skills == [f"Skill {index}" for index in range(14)]
    assert changed.salary is None
    assert changed.required_skills == job.required_skills


@pytest.mark.asyncio
async def test_hirehi_known_malformed_and_hidden_fields_are_parse_errors():
    from playwright.async_api import async_playwright

    malformed = """
      <section class="resume-public-card"><h2 class="resume-public-section-title">Experience</h2>
        <div class="resume-public-list-item"><div class="resume-public-list-subtitle">Role</div></div>
      </section>
    """
    hidden = """
      <section class="resume-public-card" data-section="education" style="display:none"><h2 class="resume-public-section-title">Education</h2><div class="resume-public-list-item">Hidden section</div></section>
      <section class="resume-public-card"><h2 class="resume-public-section-title">Experience</h2>
        <div class="resume-public-list-item"><span class="resume-public-list-company" style="display:none">Hidden Co</span><div class="resume-public-list-subtitle">Role</div></div>
      </section>
    """
    async with async_playwright() as playwright:
        browser, context, page = await _isolated_page(playwright, malformed)
        malformed_snapshot = await extractor.extract(page, POLICY.validate("https://hirehi.ru/resume/Sample_1"), POLICY)
        await page.set_content(hidden, wait_until="domcontentloaded")
        hidden_snapshot = await extractor.extract(page, POLICY.validate("https://hirehi.ru/resume/Sample_1"), POLICY)
        await context.close()
        await browser.close()

    assert any("experience" in error for error in malformed_snapshot.coverage.parse_errors)
    assert any("experience" in error for error in hidden_snapshot.coverage.parse_errors)
    assert any(section.name == "education" for section in hidden_snapshot.additional_sections)


@pytest.mark.asyncio
async def test_hirehi_duplicate_dom_id_is_removed_but_identical_real_jobs_remain():
    from playwright.async_api import async_playwright

    html = """
      <section class="resume-public-card"><h2 class="resume-public-section-title">Experience</h2>
        <div class="resume-public-list-item" data-item-id="technical-1"><span class="resume-public-list-company">Same Co</span><div class="resume-public-list-subtitle">Same Role</div></div>
        <div class="resume-public-list-item" data-item-id="technical-1"><span class="resume-public-list-company">Same Co</span><div class="resume-public-list-subtitle">Same Role</div></div>
        <div class="resume-public-list-item" data-item-id="real-2"><span class="resume-public-list-company">Same Co</span><div class="resume-public-list-subtitle">Same Role</div></div>
      </section>
    """
    async with async_playwright() as playwright:
        browser, context, page = await _isolated_page(playwright, html)
        snapshot = await extractor.extract(page, POLICY.validate("https://hirehi.ru/resume/Sample_1"), POLICY)
        await context.close()
        await browser.close()

    assert len(snapshot.experience) == 2


@pytest.mark.asyncio
async def test_hirehi_unknown_section_read_error_is_not_silently_missing(monkeypatch):
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser, context, page = await _isolated_page(playwright, """
          <section class="resume-public-card"><h2 class="resume-public-section-title">Unknown</h2><div>content</div></section>
        """)
        monkeypatch.setattr("backend.adapters.hirehi.resume._inner_text", _raise_read_error)
        with pytest.raises(RuntimeError, match="DOM read failure"):
            await extractor.extract(page, POLICY.validate("https://hirehi.ru/resume/Sample_1"), POLICY)
        await context.close()
        await browser.close()


async def _raise_read_error(_node):
    raise RuntimeError("DOM read failure")


@pytest.mark.asyncio
async def test_hirehi_real_html_contract_is_opt_in_and_pii_free():
    fixture_dir = os.getenv("LOCAL_RESUME_HTML_DIR")
    if not fixture_dir:
        pytest.skip("set LOCAL_RESUME_HTML_DIR to run the private local fixture contract")
    fixture = Path(fixture_dir) / "HireHi.html"
    if not fixture.is_file():
        pytest.skip("HireHi.html is unavailable")
    from playwright.async_api import async_playwright

    html = fixture.read_text(encoding="utf-8", errors="replace")
    async with async_playwright() as playwright:
        browser, context, page = await _isolated_page(playwright, html)
        snapshot = await extractor.extract(page, POLICY.validate("https://hirehi.ru/resume/LocalFixture_1"), POLICY)
        await context.close()
        await browser.close()

    assert len(snapshot.experience) == 3
    assert len(snapshot.education) == 1
    assert len(snapshot.awards) == 1
    assert len(snapshot.courses) == 1
    assert len(snapshot.languages) == 2
    assert [language.proficiency.value for language in snapshot.languages] == [
        "B1 — Средний уровень", "Носитель — Родной язык"
    ]
    assert len(snapshot.skills) == 24
    assert snapshot.education[0].description.availability.value == "present"
    assert snapshot.education[0].duration.availability.value == "present"
    assert snapshot.awards[0].description.availability.value == "present"
    assert snapshot.contacts.email.availability.value == "present"
    assert snapshot.contacts.phone.availability.value == "present"
    assert snapshot.location.relocation.availability.value == "present"
