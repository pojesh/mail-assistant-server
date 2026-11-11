"""LLM judge for chat answers (RAGAS-style claim checking).

The judge splits an answer into factual claims, checks each claim against the documents the
agent's tools actually returned, and checks which key facts of the reference answer are covered.

Primary judge: HCL AI Cafe GPT-4.1, a different model family from the app's, so the app's model
never grades itself. A judge needs only plain chat with JSON in the prompt, which AI Cafe supports.
If AI Cafe is not configured or refuses a request (its content filter can block the prompt-
injection emails), Gemini judges that case instead and the result records which judge was used.
"""

import json
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx
from dotenv import dotenv_values
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field, ValidationError

from app.llm.provider import get_chat_model
from app.llm.retry import call_with_retry

DOC_LIMIT = 3_000  # characters per tool result shown to the judge


class JudgedClaim(BaseModel):
    claim: str
    supported: bool
    docs: list[int] = Field(default_factory=list)


class JudgedFact(BaseModel):
    fact: str
    covered: bool


class Verdict(BaseModel):
    claims: list[JudgedClaim] = Field(default_factory=list)
    facts: list[JudgedFact] = Field(default_factory=list)


@dataclass
class Document:
    tool: str
    text: str
    cases: list[str]  # case ids this tool result contains (for grounding)


SYSTEM = """You evaluate an email assistant's answer. You get the user's QUESTION, the assistant's \
ANSWER, numbered DOCUMENTS (everything the assistant's tools returned) and KEY FACTS that a \
correct answer should contain.

1. Split the ANSWER into its factual claims about the user's emails, files or calendar. Ignore \
greetings, offers to help and general advice. "Nothing relevant was found" is a claim; it is \
supported only if the documents really contain nothing relevant.
2. For each claim set supported=true only if the DOCUMENTS state it (paraphrase is fine; never \
use outside knowledge). Put the numbers of the supporting documents in "docs".
3. For each KEY FACT set covered=true if the ANSWER states or clearly conveys it.

Reply with JSON only, no other text:
{"claims": [{"claim": "...", "supported": true, "docs": [1]}], \
"facts": [{"fact": "...", "covered": true}]}"""


def _aicafe_settings() -> dict[str, str] | None:
    env = dotenv_values(".env")
    values = {
        key: (os.environ.get(key) or env.get(key) or "").split("#")[0].strip()
        for key in ("AICAFE_API_KEY", "AICAFE_ENDPOINT", "AICAFE_DEPLOYMENT", "AICAFE_API_VERSION")
    }
    return values if all(values.values()) else None


def build_prompt(question: str, answer: str, documents: list[Document], facts: list[str]) -> str:
    docs = (
        "\n\n".join(
            f"[{number}] (tool: {doc.tool})\n{doc.text[:DOC_LIMIT]}"
            for number, doc in enumerate(documents, start=1)
        )
        or "(the assistant did not call any tool)"
    )
    key_facts = "\n".join(f"- {fact}" for fact in facts)
    return (
        f"QUESTION:\n{question}\n\nANSWER:\n{answer or '(empty answer)'}\n\n"
        f"DOCUMENTS:\n{docs}\n\nKEY FACTS:\n{key_facts}"
    )


def parse_verdict(text: str) -> Verdict:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in the judge reply")
    return Verdict.model_validate(json.loads(text[start : end + 1]))


async def _ask_aicafe(settings: dict[str, str], prompt: str) -> Verdict:
    url = (
        f"{settings['AICAFE_ENDPOINT'].rstrip('/')}/deployments/{settings['AICAFE_DEPLOYMENT']}"
        f"/chat/completions?api-version={settings['AICAFE_API_VERSION']}"
    )
    messages: list[dict[str, str]] = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": prompt},
    ]
    async with httpx.AsyncClient(timeout=120) as client:
        for attempt in range(2):
            response = await client.post(
                url,
                headers={"api-key": settings["AICAFE_API_KEY"]},
                # AI Cafe accepts only model/messages/temperature/maxTokens (camelCase).
                json={
                    "model": settings["AICAFE_DEPLOYMENT"],
                    "temperature": 0,
                    "maxTokens": 2500,
                    "messages": messages,
                },
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"] or ""
            try:
                return parse_verdict(content)
            except (ValueError, ValidationError):
                if attempt == 1:
                    raise
                messages += [
                    {"role": "assistant", "content": content},
                    {
                        "role": "user",
                        "content": "That was not valid JSON. Reply with the JSON object only.",
                    },
                ]
    raise ValueError("unreachable")


async def _ask_gemini(prompt: str) -> Verdict:
    model = get_chat_model("smart").with_structured_output(Verdict)
    messages = [SystemMessage(SYSTEM), HumanMessage(prompt)]
    result = await call_with_retry(lambda: model.ainvoke(messages), purpose="eval_judge")
    return Verdict.model_validate(result)


def _describe(error: Exception) -> str:
    """Short reason for the report, e.g. 'HTTP 400: Content filter ...' (never the API key)."""
    if isinstance(error, httpx.HTTPStatusError):
        body = " ".join(error.response.text.split())[:160]
        return f"HTTP {error.response.status_code}: {body}"
    return type(error).__name__


async def judge_answer(
    question: str, answer: str, documents: list[Document], facts: list[str]
) -> dict[str, Any]:
    """Returns {"model", "verdict", "seconds", "note"}; raises only if every judge fails."""
    prompt = build_prompt(question, answer, documents, facts)
    started = time.perf_counter()
    settings = _aicafe_settings()
    note = None
    if settings:
        try:
            verdict = await _ask_aicafe(settings, prompt)
            return {
                "model": f"aicafe/{settings['AICAFE_DEPLOYMENT']}",
                "verdict": verdict,
                "seconds": time.perf_counter() - started,
                "note": None,
            }
        except (httpx.HTTPError, ValueError, ValidationError, KeyError) as error:
            note = f"AI Cafe failed ({_describe(error)}); judged by the app's own model instead"
    else:
        note = "AI Cafe not configured; judged by the app's own model"
    verdict = await _ask_gemini(prompt)
    model = f"{os.environ.get('LLM_PROVIDER')}/{os.environ.get('LLM_SMART_MODEL')}"
    return {
        "model": model,
        "verdict": verdict,
        "seconds": time.perf_counter() - started,
        "note": note,
    }
