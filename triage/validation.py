"""Input and LLM-output validation (kept dependency-free on purpose)."""
from . import config


class ValidationError(ValueError):
    pass


def validate_issue_input(payload) -> dict:
    if not isinstance(payload, dict):
        raise ValidationError("Request body must be a JSON object.")
    title = str(payload.get("title") or "").strip()
    description = str(payload.get("description") or "").strip()
    reporter = str(payload.get("reporter") or "").strip() or None
    if len(title) < 3:
        raise ValidationError("Title must be at least 3 characters.")
    if len(title) > 200:
        raise ValidationError("Title must be 200 characters or fewer.")
    if not description:
        raise ValidationError("Description is required.")
    if len(description) > 5000:
        raise ValidationError("Description must be 5000 characters or fewer.")
    return {"title": title, "description": description, "reporter": reporter}


def validate_llm_output(data, allowed_ids) -> dict:
    """Check the model's JSON against the schema. Raises ValidationError if invalid.

    allowed_ids: ids of past issues actually shown to the model. Any other id the
    model returns is treated as a hallucination and removed.
    """
    if not isinstance(data, dict):
        raise ValidationError("LLM output is not a JSON object.")
    out = {}
    for field, allowed in (("type", config.ISSUE_TYPES),
                           ("module", config.MODULES),
                           ("priority", config.PRIORITIES)):
        value = str(data.get(field, "")).strip().lower()
        if value not in allowed:
            raise ValidationError(f"Invalid {field}: {value!r}")
        out[field] = value
    try:
        conf = float(data.get("confidence"))
    except (TypeError, ValueError):
        raise ValidationError("Confidence must be a number.")
    if not 0 <= conf <= 1:
        raise ValidationError("Confidence must be between 0 and 1.")
    out["confidence"] = conf
    out["justification"] = str(data.get("justification") or "")[:500]

    allowed_ids = set(allowed_ids)
    dup = data.get("duplicate_of")
    out["duplicate_of"] = dup if isinstance(dup, int) and dup in allowed_ids else None
    sources = data.get("recommendation_sources") or []
    out["recommendation_sources"] = [s for s in sources if isinstance(s, int) and s in allowed_ids]
    rec = data.get("recommendation")
    # A recommendation with no valid source is ungrounded, so it is dropped.
    out["recommendation"] = str(rec)[:1000] if rec and out["recommendation_sources"] else None
    return out
