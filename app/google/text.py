"""Email text cleanup: HTML to text, and removing quoted replies and signatures."""

import re

from bs4 import BeautifulSoup

# The start of a quoted earlier message. Everything from here on is dropped.
QUOTE_STARTS = [
    # Gmail / Apple Mail: "On Mon, 3 Nov 2026 at 10:00, Ravi <ravi@x.com> wrote:" (may wrap once)
    re.compile(r"^On [^\n]{0,200}(\n[^\n]{0,200})?wrote:[ \t]*$", re.MULTILINE),
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^-{2,}\s*Forwarded message\s*-{2,}", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^_{10,}\s*$\s*^From:", re.MULTILINE),  # Outlook separator line
    re.compile(r"^From: .+\n(Sent|Date): .+", re.MULTILINE),  # Outlook header block
]
# The start of a signature. Everything from here on is dropped.
SIGNATURE_STARTS = [
    re.compile(r"^-- ?$", re.MULTILINE),  # standard "-- " delimiter
    re.compile(r"^Sent from my (iPhone|iPad|Android|mobile).*$", re.MULTILINE | re.IGNORECASE),
]
MANY_BLANK_LINES = re.compile(r"\n{3,}")


def html_to_text(html: str) -> str:
    """Convert an HTML email body to readable plain text."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "head", "title"]):
        tag.decompose()
    lines = (line.strip() for line in soup.get_text("\n").splitlines())
    return MANY_BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()


def _cut_at_first(text: str, patterns: list[re.Pattern[str]]) -> str:
    """Cut the text at the earliest match of any pattern (if the match is not at the very top)."""
    positions = [match.start() for pattern in patterns if (match := pattern.search(text))]
    positions = [position for position in positions if position > 0]
    return text[: min(positions)] if positions else text


def strip_quoted_text(text: str) -> str:
    """Remove quoted earlier messages, '>' quote lines and the signature."""
    text = text.replace("\r\n", "\n")
    text = _cut_at_first(text, QUOTE_STARTS)
    text = "\n".join(line for line in text.split("\n") if not line.lstrip().startswith(">"))
    text = _cut_at_first(text, SIGNATURE_STARTS)
    return MANY_BLANK_LINES.sub("\n\n", text).strip()
