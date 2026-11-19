"""Versioned prompt files (docs/week5-plan.md B5-D9).

Each prompt lives at `app/prompts/<name>/v<N>.md`. `ACTIVE` pins the version
in use, so changing a prompt is a new file plus a one-line bump — the old
text stays for comparison, and every interaction logs `Prompt.ref`
(`assistant_rules@v1`), which ties an answer to the exact words that made it.

Prompts are plain, provider-neutral language: no provider-specific tags or
tricks, so the same file serves Anthropic and OpenAI (B5-D1).
"""

import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path

_DIR = Path(__file__).parent

# name -> version in use. Bump here after adding v<N+1>.md.
ACTIVE: dict[str, int] = {
    "assistant_rules": 1,
    "understand": 2,
    "pick": 2,
    "explain": 1,
    "summarize": 1,
    "order_explain": 1,
    "content_rules": 1,
    "content_description": 1,
    "content_promotion": 1,
    "content_dishes": 1,
    "content_message": 1,
    "content_feedback_batch": 1,
    "content_feedback_combine": 1,
}


@dataclass(frozen=True)
class Prompt:
    name: str
    version: int
    text: str

    @property
    def ref(self) -> str:
        return f"{self.name}@v{self.version}"


@cache
def load(name: str, version: int | None = None) -> Prompt:
    """The prompt's text; the active version unless one is asked for."""
    v = version if version is not None else ACTIVE[name]
    path = _DIR / name / f"v{v}.md"
    return Prompt(name=name, version=v, text=path.read_text(encoding="utf-8").strip())


_LABEL = re.compile(r"^[a-z][a-z0-9_]*$")


def fence(label: str, text: str) -> str:
    """Wraps untrusted text (reviews, the customer's message, menu text
    written by restaurants) in `<label>…</label>`, the form the rules prompt
    tells the model to treat as data, never as instructions.

    Any tag in the text that would open or close a fence is defused (`<` →
    `‹`), so a review can't end its fence early and talk to the model as if
    it were us."""
    if not _LABEL.match(label):
        raise ValueError(f"Bad fence label {label!r}")
    defused = re.sub(r"<(/?\s*[a-zA-Z_][\w-]*)", r"‹\1", text)
    return f"<{label}>\n{defused}\n</{label}>"
