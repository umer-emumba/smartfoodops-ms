"""Redaction for anything the AI layer stores (docs/week5-plan.md B5-D8).

The LLM is never *given* names, phones or addresses — prompts are built from
IDs, menu data and coordinates resolved server-side. But a customer can type
their own phone number or email into the chat, and conversation turns are
stored in ai_db, so stored text goes through `redact()` first.

Pattern-based, so it catches the common shapes, not every possible one: a
safety net, not the privacy control itself.
"""

import re

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# Card-like: 13–19 digits, optionally grouped by spaces or dashes. Checked
# before phones, which would otherwise eat part of it.
_CARD = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")
# Pakistani mobiles (03xx-xxxxxxx, +92 3xx xxxxxxx) and other long numbers
# written like phones: 10+ digits with optional +, spaces, dashes, brackets.
_PHONE = re.compile(r"(?<![\w.])\+?\(?\d[\d\s()-]{8,}\d(?![\w.])")


def redact(text: str) -> str:
    text = _EMAIL.sub("[email]", text)
    text = _CARD.sub("[card number]", text)
    return _PHONE.sub(lambda m: "[phone]" if _digits(m.group()) >= 10 else m.group(), text)


def _digits(s: str) -> int:
    return sum(ch.isdigit() for ch in s)
