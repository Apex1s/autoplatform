"""Strict-JSON LLM calls: JSON mode + schema validation + one repair round (mirrors worker/src/llmjson.ts).

Schemas use a small JSON-Schema subset: type (incl. lists like ["string", "null"]), required,
properties, items, enum, minimum, maximum. Values are coerced where unambiguous ("87" -> 87,
87.0 -> 87) and numbers are clamped to [minimum, maximum]; anything else is a validation error.
"""
from __future__ import annotations

import json
import re
from typing import Any

from core.llm_router import AllProvidersFailed

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.I)


class SchemaError(ValueError):
    pass


def _types(schema: dict) -> list[str]:
    t = schema.get("type")
    return t if isinstance(t, list) else ([t] if t else [])


def coerce(schema: dict, value: Any, path: str = "$") -> Any:
    types = _types(schema)
    if value is None:
        if not types or "null" in types:
            return None
        raise SchemaError(f"{path}: required value is null")
    if "enum" in schema:
        if isinstance(value, str):
            match = next((e for e in schema["enum"] if isinstance(e, str) and e.lower() == value.strip().lower()), None)
            if match is not None:
                return match
        if value in schema["enum"]:
            return value
        raise SchemaError(f"{path}: {value!r} not in {schema['enum']}")
    if "object" in types and isinstance(value, dict):
        out = dict(value)
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in out:
                if "null" in _types(props.get(key, {})):
                    out[key] = None
                else:
                    raise SchemaError(f"{path}.{key}: missing")
        for key, sub in props.items():
            if key in out:
                out[key] = coerce(sub, out[key], f"{path}.{key}")
        return out
    if "array" in types and isinstance(value, list):
        sub = schema.get("items", {})
        return [coerce(sub, v, f"{path}[{i}]") for i, v in enumerate(value)]
    if ("integer" in types or "number" in types) and not isinstance(value, bool):
        num = value
        if isinstance(value, str):
            try:
                num = float(value.strip().rstrip("%"))
            except ValueError:
                num = None
        if isinstance(num, (int, float)):
            if "minimum" in schema:
                num = max(schema["minimum"], num)
            if "maximum" in schema:
                num = min(schema["maximum"], num)
            return int(round(num)) if "integer" in types else float(num)
    if "boolean" in types:
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            return value.strip().lower() == "true"
    if "string" in types:
        if isinstance(value, str):
            return value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
    raise SchemaError(f"{path}: expected {'/'.join(types)}, got {type(value).__name__}")


def parse_json_object(text: str) -> dict:
    t = _FENCE.sub("", text.strip())
    try:
        obj = json.loads(t)
    except ValueError:
        start, end = t.find("{"), t.rfind("}")
        if start < 0 or end <= start:
            raise SchemaError("response is not JSON")
        obj = json.loads(t[start:end + 1])
    if not isinstance(obj, dict):
        raise SchemaError("response is not a JSON object")
    return obj


def describe(schema: dict, indent: int = 0) -> str:
    """Compact, model-friendly description of the expected JSON shape."""
    pad = "  " * indent
    types = _types(schema)
    if "enum" in schema:
        return " | ".join(json.dumps(e) for e in schema["enum"])
    if "object" in types:
        lines = ["{"]
        for key, sub in schema.get("properties", {}).items():
            desc = sub.get("description", "")
            lines.append(f'{pad}  "{key}": {describe(sub, indent + 1)}{"  // " + desc if desc else ""}')
        lines.append(pad + "}")
        return "\n".join(lines)
    if "array" in types:
        return f"[{describe(schema.get('items', {}), indent)}, ...]"
    rng = ""
    if "minimum" in schema or "maximum" in schema:
        rng = f" ({schema.get('minimum', '')}..{schema.get('maximum', '')})"
    return "/".join(t for t in types) + rng


async def complete_json(ctx, *, system: str, user: str, schema: dict, max_tokens: int = 700,
                        providers: list[str] | None = None, simulate_outage: str | None = None) -> tuple[dict, dict]:
    """Returns (validated_object, llm_meta). Raises AllProvidersFailed / SchemaError (both retryable).
    `providers` restricts which LLMs may see this client's data (e.g. ["groq"] for PII-sensitive clients)."""
    only = providers or None
    simulate = simulate_outage
    messages = [
        {"role": "system", "content": system + "\n\nRespond with ONE JSON object only, no prose, matching:\n"
                                       + describe(schema)},
        {"role": "user", "content": user},
    ]
    attempts: list[dict] = []
    last_error = ""
    for round_ in range(2):  # original + one repair round
        res = await ctx.router.complete(messages, max_tokens=max_tokens, temperature=0.2, only=only,
                                        client_id=ctx.client_id, job_id=ctx.job_id,
                                        response_format={"type": "json_object"},
                                        simulate_failure=simulate if round_ == 0 else None)
        attempts.extend(res.attempts)
        try:
            data = coerce(schema, parse_json_object(res.text))
            meta = {"provider": res.provider, "model": res.model, "latency_ms": res.latency_ms,
                    "prompt_tokens": sum(a.get("prompt_tokens", 0) for a in attempts),
                    "completion_tokens": sum(a.get("completion_tokens", 0) for a in attempts),
                    "attempts": [{k: a.get(k) for k in ("provider", "success", "error_kind", "status_code",
                                                         "latency_ms", "simulated") if k in a} for a in attempts],
                    "failover": any(not a.get("success") for a in attempts),
                    "repaired": round_ > 0}
            return data, meta
        except (SchemaError, ValueError) as e:
            last_error = str(e)
            messages = messages + [{"role": "assistant", "content": res.text[:4000]},
                                   {"role": "user", "content": f"That was invalid ({last_error}). "
                                                               "Reply with the corrected JSON object only."}]
    raise SchemaError(f"model output failed validation twice: {last_error}")


__all__ = ["complete_json", "coerce", "parse_json_object", "describe", "SchemaError", "AllProvidersFailed"]
