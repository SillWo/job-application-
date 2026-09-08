from __future__ import annotations

import json

from openai import APIStatusError


async def create_completion(client, **kwargs):
    """Negotiate only explicitly rejected options; callers still validate JSON locally."""
    request = dict(kwargs)
    changed: set[str] = set()
    for _ in range(5):
        try:
            return await client.chat.completions.create(**request)
        except APIStatusError as exc:
            if exc.status_code not in {400, 422}:
                raise
            body = exc.body if isinstance(exc.body, dict) else {}
            error = body.get("error", body)
            if not isinstance(error, dict):
                raise
            detail = str(error.get("message", "")).lower()
            param = str(error.get("param", "")).lower()
            unsupported = any(word in detail for word in (
                "not supported", "unsupported", "does not support", "unknown parameter",
                "unrecognized", "not allowed", "only the default",
            ))
            if not unsupported:
                raise
            if "max_tokens" in (param + detail) and "max_tokens" in request and "tokens" not in changed:
                request["max_completion_tokens"] = request.pop("max_tokens")
                changed.add("tokens")
            elif "temperature" in (param + detail) and "temperature" in request:
                request.pop("temperature")
            elif any(name in (param + detail) for name in ("response_format", "json_schema", "json_object")):
                fmt = request.get("response_format", {})
                if fmt.get("type") == "json_schema":
                    contract = json.dumps(fmt["json_schema"]["schema"], ensure_ascii=False)
                    request["messages"] = [dict(message) for message in request["messages"]]
                    request["messages"][0]["content"] += f" Return only JSON matching this JSON Schema: {contract}"
                    request["response_format"] = {"type": "json_object"}
                elif fmt.get("type") == "json_object":
                    request.pop("response_format")
                else:
                    raise
            else:
                raise
    raise RuntimeError("Не удалось согласовать параметры OpenAI API")
