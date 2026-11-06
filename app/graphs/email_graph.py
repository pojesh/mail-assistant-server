"""The email graph (spec §11.2): triage -> labels -> scheduling and/or drafting -> approval.

    START -> guardrail_input -> triage -> apply_labels -> route
      route: suspicious / newsletter / promotion / nothing to do -> END
             otherwise -> retrieve_context
      retrieve_context -> scheduling_agent -> validate_decision -> autonomy_gate
                          (auto)     -> execute_calendar_action -> notify -> [draft] -> END
                          (approval) -> [draft_replies -> guardrail_output] -> create_approval
                       -> draft_replies -> guardrail_output -> create_approval
      create_approval -> await_approval (interrupt) -> execute_decision -> [learn_from_edit] -> END

Manual flows ("Draft reply" / "Find a time", or chat) start at retrieve_context.
"""

from typing import Any, Literal

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.graphs import nodes_approval, nodes_draft, nodes_execute, nodes_schedule, nodes_triage
from app.graphs.state import EmailState


def route_start(state: EmailState) -> Literal["guardrail_input", "retrieve_context"]:
    return "guardrail_input" if state.get("flow", "triage") == "triage" else "retrieve_context"


def build_graph() -> StateGraph[EmailState]:
    """Declare nodes and edges. Only agent nodes call an LLM; the rest is plain code."""
    graph = StateGraph(EmailState)
    nodes: dict[str, Any] = {
        "guardrail_input": nodes_triage.guardrail_input_node,
        "triage": nodes_triage.triage_node,
        "apply_labels": nodes_triage.apply_labels_node,
        "retrieve_context": nodes_draft.retrieve_context_node,
        "scheduling_agent": nodes_schedule.scheduling_agent_node,
        "validate_decision": nodes_schedule.validate_decision_node,
        "autonomy_gate": nodes_schedule.autonomy_gate_node,
        "execute_calendar_action": nodes_schedule.execute_calendar_action_node,
        "notify": nodes_schedule.notify_node,
        "draft_replies": nodes_draft.draft_replies_node,
        "guardrail_output": nodes_draft.guardrail_output_node,
        "create_approval": nodes_approval.create_approval_node,
        "await_approval": nodes_approval.await_approval_node,
        "execute_decision": nodes_execute.execute_decision_node,
        "learn_from_edit": nodes_execute.learn_from_edit_node,
    }
    for name, function in nodes.items():
        graph.add_node(name, function)

    graph.add_conditional_edges(START, route_start)
    graph.add_edge("guardrail_input", "triage")
    graph.add_edge("triage", "apply_labels")
    graph.add_conditional_edges("apply_labels", nodes_triage.route_after_labels)
    graph.add_conditional_edges("retrieve_context", nodes_draft.route_after_context)
    graph.add_edge("scheduling_agent", "validate_decision")
    graph.add_edge("validate_decision", "autonomy_gate")
    graph.add_conditional_edges("autonomy_gate", nodes_schedule.route_after_gate)
    graph.add_conditional_edges("execute_calendar_action", nodes_schedule.route_after_execute)
    graph.add_conditional_edges("notify", nodes_schedule.route_after_notify)
    graph.add_edge("draft_replies", "guardrail_output")
    graph.add_conditional_edges("guardrail_output", nodes_draft.route_after_output_check)
    graph.add_edge("create_approval", "await_approval")
    graph.add_edge("await_approval", "execute_decision")
    graph.add_conditional_edges("execute_decision", nodes_approval.route_after_execute_decision)
    graph.add_edge("learn_from_edit", END)
    return graph


_compiled_graph: CompiledStateGraph[EmailState] | None = None


def init_email_graph(checkpointer: BaseCheckpointSaver[Any]) -> None:
    """Compile the graph once at startup with the Postgres (or in-memory, in tests) checkpointer."""
    global _compiled_graph
    _compiled_graph = build_graph().compile(checkpointer=checkpointer)


def get_email_graph() -> CompiledStateGraph[EmailState]:
    if _compiled_graph is None:
        raise RuntimeError("The email graph is not initialised (init_email_graph at startup).")
    return _compiled_graph
