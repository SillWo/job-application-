"""HH questionnaire DOM mapping and verified filling, without candidate reasoning."""
from __future__ import annotations

import hashlib
import re

from backend.adapters.base.protocol import ApplicationForm, FillResult
from backend.schemas.domain import ApplicationField, ApplicationPlan

from . import locators


async def _visible_text(locator) -> str:
    for index in range(await locator.count()):
        candidate = locator.nth(index)
        if await candidate.is_visible():
            return " ".join((await candidate.inner_text()).split())
    return ""


async def _label(page, control) -> str:
    aria = await control.get_attribute("aria-label")
    if aria:
        return aria.strip()
    labelled_by = await control.get_attribute("aria-labelledby")
    if labelled_by:
        labels = [await _visible_text(page.locator(locators.attribute_selector("", "id", ident)))
                  for ident in labelled_by.split()]
        if any(labels):
            return " ".join(labels).strip()
    ident = await control.get_attribute("id")
    if ident:
        label = await _visible_text(page.locator(locators.attribute_selector("label", "for", ident)))
        if label:
            return label
    return await _visible_text(control.locator(locators.WRAPPING_LABEL))


async def _is_application_control(page, control, name: str) -> bool:
    if name.startswith("task_"):
        return True
    roots = page.locator(locators.APPLICATION_ROOT)
    for index in range(await roots.count()):
        root = roots.nth(index)
        if await root.is_visible() and await root.locator(locators.APPLICATION_CONTROL).and_(control).count():
            return True
    return False


async def controls(page) -> list[tuple[ApplicationField, list]]:
    """Build groups by HH task name; hidden/disabled controls do not shift prompts."""
    result: list[tuple[ApplicationField, list]] = []
    grouped: dict[str, tuple[ApplicationField, list]] = {}
    nodes = page.locator(locators.APPLICATION_CONTROL)
    for index in range(await nodes.count()):
        node = nodes.nth(index)
        if not await node.is_visible() or not await node.is_enabled():
            continue
        name = (await node.get_attribute("name")) or ""
        qa = (await node.get_attribute("data-qa")) or ""
        type_ = ((await node.get_attribute("type")) or "text").lower()
        if type_ in {"hidden", "submit", "button", "reset", "image"}:
            continue
        if any(part in (name + " " + qa).lower() for part in (
            "cover_letter", "coverletter", "letter", "csrf", "xsrf", "captcha", "resume",
        )):
            continue
        if not await _is_application_control(page, node, name):
            continue
        own_label = await _label(page, node)
        task = node.locator(locators.TASK_CONTAINER)
        task_prompt = ""
        if await task.count():
            prompts = task.locator(locators.TASK_QUESTION)
            if await prompts.count() == 1:
                task_prompt = await _visible_text(prompts)
        legend = await _visible_text(node.locator(locators.FIELDSET).locator(locators.LEGEND))
        label = task_prompt or legend or own_label
        if not label:
            placeholder = (await node.get_attribute("placeholder")) or ""
            if placeholder.lower() not in {"", "писать тут", "ответ", "ваш ответ"}:
                label = placeholder
        label = label or f"Вопрос без подписи ({name or index})"
        if "сопровод" in label.casefold():
            continue
        kind = "text"
        if not await node.locator(locators.IS_NATIVE_CONTROL).count():
            kind = "unsupported"
        elif await node.locator(locators.IS_SELECT).count():
            kind = "multiselect" if await node.get_attribute("multiple") is not None else "select"
        elif type_ in {"radio", "checkbox", "number"}:
            kind = type_
        elif type_ not in {"text", "email", "tel", "url"} and not await node.locator(locators.IS_TEXTAREA).count():
            kind = "unsupported"
        ident = name or (await node.get_attribute("id")) or f"control-{index}"
        # Checkbox names can be task_N_checkbox_0 etc; group by the task ID.
        task_id = re.match(r"task_\d+", name)
        if kind in {"radio", "checkbox"} and task_id:
            ident = task_id.group()
        key = f"{kind}:{ident}:" + hashlib.sha256(label.encode()).hexdigest()[:16]
        if kind in {"radio", "checkbox"} and key in grouped:
            field, members = grouped[key]
            field.options.append(own_label or (await node.get_attribute("value")) or "")
            members.append(node)
            continue
        options: list[str] = []
        if kind in {"radio", "checkbox"}:
            options = [own_label or (await node.get_attribute("value")) or ""]
        elif kind in {"select", "multiselect"}:
            option_nodes = node.locator(locators.OPTION)
            for option in await option_nodes.all():
                if await option.is_enabled() and await option.get_attribute("value") != "":
                    options.append((await option.inner_text()).strip())
        maxlength = await node.get_attribute("maxlength")
        field = ApplicationField(
            id=key, label=label, kind=kind, options=options,
            # HH often omits native required; employer tasks are mandatory.
            required=name.startswith("task_") or await node.get_attribute("required") is not None
            or await node.get_attribute("aria-required") != "false",
            max_length=int(maxlength) if maxlength and maxlength.isdigit() else None,
        )
        entry = (field, [node])
        result.append(entry)
        grouped[key] = entry
    return result


async def read_form(page) -> ApplicationForm:
    fields = [field for field, _ in await controls(page)]
    # Read the specific button only inside the exact country notice.
    confirmation = None
    notice = page.get_by_text(locators.FOREIGN_NOTICE, exact=False)
    if await notice.count() and await notice.first.is_visible():
        confirmation = "foreign_country"
    return ApplicationForm(
        fields=fields, questions=[field.label for field in fields if field.required],
        requires_cover_letter=bool(await page.locator(locators.COVER_LETTER_INPUT).count()
                                   or await page.locator(locators.COVER_LETTER_TOGGLE).count()),
        confirmation=confirmation,
    )


async def fill_fields(page, plan: ApplicationPlan) -> FillResult:
    answered: list[str] = []
    unknown: list[str] = []
    for field, nodes in await controls(page):
        answer = plan.form_answers.get(field.id)
        if answer is None or answer.field != field or not answer.values:
            if field.required:
                unknown.append(field.label)
            continue
        values = answer.values
        if (field.max_length is not None and any(len(value) > field.max_length for value in values)
                or field.kind == "unsupported"
                or field.kind not in {"checkbox", "multiselect"} and len(values) != 1
                or field.options and (len(set(field.options)) != len(field.options)
                                     or any(value not in field.options for value in values))):
            unknown.append(field.label)
            continue
        node = nodes[0]
        try:
            if field.kind in {"select", "multiselect"}:
                await node.select_option(label=values if field.kind == "multiselect" else values[0])
                actual = [text.strip() for text in await node.locator(locators.SELECTED_OPTIONS).all_text_contents()]
                verified = set(actual) == set(values)
            elif field.kind in {"checkbox", "radio"}:
                for option, member in zip(field.options, nodes, strict=True):
                    if field.kind == "checkbox":
                        await member.set_checked(option in values)
                    elif option in values:
                        await member.check()
                actual = [option for option, member in zip(field.options, nodes, strict=True) if await member.is_checked()]
                verified = set(actual) == set(values)
            else:
                await node.fill(values[0])
                verified = (await node.input_value()).strip() == values[0].strip()
            if verified and await node.get_attribute("aria-invalid") != "true":
                answered.append(field.id)
            else:
                unknown.append(field.label)
        except Exception:
            # A changed/unfillable field never becomes evidence of completion.
            unknown.append(field.label)
    return FillResult(success=not unknown, answered_fields=answered, unknown_questions=unknown)
