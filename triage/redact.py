"""Removes sensitive data before text is stored or sent to an external AI service."""
import re

# Order matters: more specific patterns run first.
_PATTERNS = [
    ("EMAIL", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
    ("AWS_KEY", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("API_KEY", re.compile(r"\b(?:sk|pk|rk)[-_][A-Za-z0-9_-]{16,}\b")),
    ("BEARER_TOKEN", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]+=*")),
    ("CARD_NUMBER", re.compile(r"\b(?:\d[ -]?){12,15}\d\b")),
    ("PHONE", re.compile(r"(?<![\w-])\+?\d[\d -]{8,13}\d(?![\w-])")),
]
_SECRET_ASSIGN = re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key)(\s*[:=]\s*)(\S+)")


def redact(text: str):
    """Return (redacted_text, number_of_redactions)."""
    if not text:
        return text, 0
    total = 0
    text, n = _SECRET_ASSIGN.subn(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED_SECRET]", text)
    total += n
    for name, pattern in _PATTERNS:
        text, n = pattern.subn(f"[REDACTED_{name}]", text)
        total += n
    return text, total
