"""Chat scenarios: tool use and RAG quality of the real chat agent on a synthetic mailbox.

For every question:
- Tool use: did the agent call the right tools (any-of groups), and no forbidden ones?
- Retrieval: context precision/recall (every email that reached the model through a tool) and
  citation precision/recall (the sources shown to the user), against the known relevant threads.
- Answer (LLM judge, see judge.py): faithfulness = share of the answer's claims supported by
  what the tools returned; grounding = share of claims supported by an email that is actually
  cited; correctness = share of the reference key facts the answer contains.

A case passes when the tools are right, faithfulness >= 0.8 and correctness >= 0.66.
"""

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import HumanMessage, ToolMessage

from app.agents.chat import CitationTracker, run_chat
from app.errors import AppError
from app.schemas.chat import ChatStepEvent
from app.services import chat as chat_service
from app.services.preferences import load_preferences
from app.tools.context import AgentContext
from tests.evals import scheduling_eval, triage_eval
from tests.evals.dates import render
from tests.scenarios.conftest import measure, truncate_all
from tests.scenarios.judge import Document, judge_answer
from tests.scenarios.mailbox import Mailbox, case_id_of, create_user
from tests.scenarios.recorder import Recorder

DATA = json.loads((Path(__file__).parent / "data" / "chat_cases.json").read_text(encoding="utf-8"))
CASES: list[dict[str, Any]] = DATA["cases"]
PROFILE, _, DAYS = scheduling_eval.load(None)
HAND_OFF_TOOLS = ["start_draft_flow", "start_schedule_flow"]
CITED_KINDS = {"mail", "safety"}  # answers that should cite received emails


@dataclass
class ChatWorld:
    context: AgentContext
    email_cases: dict[str, str]  # stored email id -> case id
    mailbox: Mailbox


@dataclass
class RecordingTracker(CitationTracker):
    """The app's citation tracker, also keeping every tool result for scoring."""

    results: list[tuple[str, str]] = field(default_factory=list)

    def add(self, message: ToolMessage) -> None:
        self.results.append((message.name or "", str(message.content)))
        super().add(message)


@pytest.fixture(scope="module")
async def chat_world(scenario_database: None) -> AsyncIterator[ChatWorld]:
    user = await create_user(PROFILE["meeting_preferences"])
    mailbox = Mailbox.build(triage_eval.load_cases(None), DAYS)
    email_cases = await mailbox.seed(user.id)  # embeds the sent replies (real embedding calls)
    prefs = await load_preferences(user.id)
    with pytest.MonkeyPatch.context() as patch:
        mailbox.install(patch)
        yield ChatWorld(chat_service.agent_context(user.id, prefs), email_cases, mailbox)
    await truncate_all()


def cases_in(content: str, email_cases: dict[str, str]) -> list[str]:
    """Case ids mentioned in a tool result (thread ids and stored email ids), in order."""
    try:
        data: Any = json.loads(content)
    except ValueError:
        return []
    found: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            thread = value.get("thread_id")
            if isinstance(thread, str) and thread.startswith("th-"):
                found.append(case_id_of(thread))
            email_id = value.get("email_id")
            if isinstance(email_id, str) and email_id in email_cases:
                found.append(email_cases[email_id])
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(data)
    return list(dict.fromkeys(found))


def share(part: set[str], whole: list[str] | set[str]) -> float:
    return len(part) / len(whole) if whole else 0.0


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
async def test_chat(case: dict[str, Any], chat_world: ChatWorld, recorder: Recorder) -> None:
    question = render(case["question"], DAYS)
    facts = [render(fact, DAYS) for fact in case["facts"]]
    row: dict[str, Any] = {"id": case["id"], "question": question, "kind": case["kind"]}
    tracker = RecordingTracker()
    used: list[str] = []
    parts: list[str] = []
    first_token: tuple[float, int] | None = None
    queries_before = len(chat_world.mailbox.queries)
    try:
        with measure() as meter:
            async for event in run_chat([HumanMessage(question)], chat_world.context, tracker):
                if isinstance(event, ChatStepEvent):
                    used.append(event.tool)
                else:
                    if first_token is None:
                        first_token = (meter.net(), meter.calls)
                    parts.append(event.text)
    except AppError as error:
        row["error"] = error.code
        recorder.add("chat", row)
        pytest.fail(f"{case['id']}: chat failed ({error.code})")
    answer = "".join(parts).strip()
    recorder.time("chat", "Full answer", meter)
    if first_token is not None:
        recorder.sample("chat", "Time to first answer token", first_token[0], first_token[1])

    # Tool use
    forbidden = case.get("forbidden_tools", HAND_OFF_TOOLS)
    required: list[list[str]] = case["required_tools"]
    forbidden_used = [tool for tool in used if tool in forbidden]
    tools = {
        "used": used,
        "required": required,
        "forbidden_used": forbidden_used,
        "ok": all(any(tool in used for tool in group) for group in required) and not forbidden_used,
        "first_ok": bool(used) and used[0] in required[0],
        "gmail_queries": chat_world.mailbox.queries[queries_before:],
    }

    # Retrieval
    documents = [
        Document(tool=name, text=content, cases=cases_in(content, chat_world.email_cases))
        for name, content in tracker.results
    ]
    retrieved = list(dict.fromkeys(c for doc in documents for c in doc.cases))
    cited = [case_id_of(citation.thread_id) for citation in tracker.best()]
    relevant = set(case["relevant"])
    retrieval: dict[str, Any] = {
        "relevant": sorted(relevant),
        "retrieved": retrieved,
        "cited": cited,
    }
    if relevant:
        hits = set(retrieved) & relevant
        retrieval["context_precision"] = share(hits, retrieved)
        retrieval["context_recall"] = share(hits, relevant)
        if case["kind"] in CITED_KINDS:
            cited_hits = set(cited) & relevant
            retrieval["citation_precision"] = share(cited_hits, cited)
            retrieval["citation_recall"] = share(cited_hits, relevant)

    # Answer quality (judge)
    judged = await judge_answer(question, answer, documents, facts)
    verdict = judged["verdict"]
    claims = verdict.claims
    supported = [claim for claim in claims if claim.supported]
    judge: dict[str, Any] = {
        "model": judged["model"],
        "note": judged["note"],
        "seconds": judged["seconds"],
        "faithfulness": len(supported) / len(claims) if claims else 1.0,
        "correctness": share(
            {f.fact for f in verdict.facts if f.covered}, {f.fact for f in verdict.facts}
        ),
        "claims": [claim.model_dump() for claim in claims],
        "facts": [fact.model_dump() for fact in verdict.facts],
    }
    if case["kind"] in CITED_KINDS:
        cited_set = set(cited)
        grounded = [
            claim
            for claim in supported
            if any(
                1 <= number <= len(documents) and cited_set & set(documents[number - 1].cases)
                for number in claim.docs
            )
        ]
        judge["grounding"] = len(grounded) / len(claims) if claims else 1.0

    passed = tools["ok"] and judge["faithfulness"] >= 0.8 and judge["correctness"] >= 0.66
    row.update(answer=answer, tools=tools, retrieval=retrieval, judge=judge, passed=passed)
    recorder.add("chat", row)
    assert passed, (
        f"{case['id']}: tools ok={tools['ok']} (used {used}, needed {required}), "
        f"faithfulness={judge['faithfulness']:.2f}, correctness={judge['correctness']:.2f}\n"
        f"answer: {answer[:400]}"
    )
