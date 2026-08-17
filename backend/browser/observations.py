async def collect_observation(page) -> dict:
    body = page.locator("body")
    return {
        "url": page.url,
        "title": await page.title(),
        "visible_text": (await body.inner_text())[:12000],
        "interactive_elements": await page.locator("a,button,input,select,textarea").evaluate_all("els => els.slice(0,100).map(e => ({tag:e.tagName, text:(e.innerText||e.getAttribute('aria-label')||'').slice(0,200)}))"),
    }

