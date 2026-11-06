"""Load prompt templates from the `prompts/` folder (one Markdown file per prompt)."""

from functools import lru_cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"


@lru_cache
def load_prompt(name: str) -> str:
    """Return the text of prompts/<name>.md."""
    path = PROMPTS_DIR / f"{name}.md"
    if not path.is_file():
        raise FileNotFoundError(f"Prompt not found: {path}")
    return path.read_text(encoding="utf-8").strip()


def render_prompt(name: str, **values: str) -> str:
    """Load a prompt and fill its {{placeholders}}. Unknown or missing names raise errors.

    Only trusted values (settings, preferences) go into templates; email text is always
    sent separately as data.
    """
    text = load_prompt(name)
    for key, value in values.items():
        marker = "{{" + key + "}}"
        if marker not in text:
            raise KeyError(f"Prompt {name!r} has no placeholder {marker}")
        # User-written values (tone, notes) must not be able to add new placeholders.
        text = text.replace(marker, value.replace("{{", "{ {"))
    if "{{" in text:
        raise KeyError(f"Prompt {name!r} still has unfilled placeholders")
    return text
