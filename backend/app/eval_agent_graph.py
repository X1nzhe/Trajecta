"""LangGraph-style Eval Agent loop and trace persistence.

The public entry points in this module keep the graph boundary small:
preprocess the run into a digest, run a tool-calling loop, and persist one
``AgentTrace`` per run. The loop uses LangChain message objects when available
and falls back to small local message shims for offline tests.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Literal, Protocol, TypedDict

from pydantic import BaseModel

from backend.app import preprocess, storage, tools
from backend.app.schemas import AgentTrace, AgentTraceEvent, TurnMetrics

try:  # pragma: no cover - exercised when optional production deps are present
    from langgraph.graph import END, START, StateGraph
except ImportError:  # pragma: no cover - local no-dependency test fallback
    END = "__end__"
    START = "__start__"
    StateGraph = None

try:  # pragma: no cover - exercised when optional production deps are present
    from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
except ImportError:  # pragma: no cover - the fallback is exercised in local tests
    AnyMessage = Any

    class _FallbackMessage:
        def __init__(self, content: str = "", **kwargs: Any) -> None:
            self.content = content
            for key, value in kwargs.items():
                setattr(self, key, value)

    class AIMessage(_FallbackMessage):
        def __init__(self, content: str = "", tool_calls: list[dict[str, Any]] | None = None, **kwargs: Any) -> None:
            super().__init__(content, tool_calls=tool_calls or [], **kwargs)

    class HumanMessage(_FallbackMessage):
        pass

    class SystemMessage(_FallbackMessage):
        pass

    class ToolMessage(_FallbackMessage):
        def __init__(
            self,
            content: str = "",
            *,
            name: str | None = None,
            tool_call_id: str | None = None,
            **kwargs: Any,
        ) -> None:
            super().__init__(content, name=name, tool_call_id=tool_call_id, **kwargs)


class EvalState(TypedDict):
    run_id: str
    user_intent: Literal["analyze_run", "analyze_step"]
    selected_step: int | None
    trajectory_digest: list[dict[str, Any]]
    messages: list[AnyMessage]
    tool_call_count: int
    eval_case_draft: dict[str, Any] | None
    errors: list[str]


class AgentLLM(Protocol):
    def invoke(self, messages: list[AnyMessage]) -> AnyMessage: ...


class GraphState(EvalState, total=False):
    trace: AgentTrace
    turn: int
    budget: int
    per_turn_budgeted_calls: int
    pending_tool_calls: list[dict[str, Any]]
    active_tool_call: dict[str, Any] | None
    llm_client: AgentLLM | Any | None
    done: bool


@dataclass
class AgentExecutionResult:
    trace: AgentTrace
    eval_case_draft: dict[str, Any] | None
    new_events: list[AgentTraceEvent]
    errors: list[str]


@dataclass
class AgentStreamDone:
    result: AgentExecutionResult


@dataclass
class AgentDelta:
    """Transient streaming chunk — text portion of an in-flight
    agent message. NOT persisted to trace; only goes over the wire
    to give the UI a token-by-token typewriter effect. The full
    agent_message event still lands in the trace at end-of-stream
    (built up by LangChain's aggregation when streaming=True), so
    everything downstream of trace persistence keeps working.

    stream_id is the LangChain AIMessageChunk.id — stable across all
    chunks of a single LLM generation, so the frontend can group
    deltas back into one bubble (matters if a turn contains
    text→tool→text and produces two separate streams).
    """

    turn: int
    text: str
    stream_id: str


AgentStreamItem = AgentTraceEvent | AgentStreamDone | AgentDelta


class NoPriorTraceError(RuntimeError):
    """Raised when a follow-up is requested before an initial analyze."""


BUDGETED_TOOLS = {
    "get_step_detail",
    "search_failure_memory",
    "search_eval_cases",
    "find_similar_successful_run",
}
SEARCH_TOOLS = {"search_failure_memory", "search_eval_cases"}
TERMINAL_TOOL = "propose_eval_case"
INITIAL_BUDGET = 8
# Followup runs the same loop as the initial analyze; giving it the same
# budget lets a single followup do a full re-analysis (e.g. user asks the
# agent to reconsider with a hint, agent re-inspects N steps and revises
# the draft). Was 4 historically — bumped to 8 once we saw real followups
# routinely needing get_step_detail + a fresh search pair.
FOLLOWUP_BUDGET = 8
_SENSITIVE_RESULT_KEYS = {"screenshot_bytes", "image_bytes", "image_data"}

_TOOL_REGISTRY = {
    "get_run": tools.get_run,
    "get_step_detail": tools.get_step_detail,
    "search_failure_memory": tools.search_failure_memory,
    "search_eval_cases": tools.search_eval_cases,
    "find_similar_successful_run": tools.find_similar_successful_run,
    "propose_eval_case": tools.propose_eval_case,
}


def preprocess_node(state: EvalState) -> EvalState:
    digest = preprocess.load_or_build_digest(state["run_id"])
    state["trajectory_digest"] = [step.model_dump(mode="json") for step in digest.steps]
    if not state["messages"]:
        state["messages"] = _initial_messages(state, followup=False)
    return state


def analyze_run(
    run_id: str,
    *,
    llm_client: AgentLLM | Any | None = None,
    budget: int = INITIAL_BUDGET,
    persist: bool = True,
) -> AgentExecutionResult:
    return _consume_stream(
        stream_analyze_run(
            run_id,
            llm_client=llm_client,
            budget=budget,
            persist=persist,
        )
    )


def stream_analyze_run(
    run_id: str,
    *,
    llm_client: AgentLLM | Any | None = None,
    budget: int = INITIAL_BUDGET,
    persist: bool = True,
) -> Iterator[AgentStreamItem]:
    """Analyze the full trajectory.

    There is no per-step entry point. The agent always works against the
    entire trajectory_digest and decides which steps to deep-inspect.
    Failure attribution is the agent's responsibility, surfaced as
    ``EvalCase.failure_step``. New traces always carry
    ``user_intent="analyze_run"`` and ``selected_step=None``; the
    ``selected_step`` field is retained in the schema only for back-compat
    reading of older traces from disk.
    """

    yield from stream_analyze(
        run_id,
        user_intent="analyze_run",
        selected_step=None,
        llm_client=llm_client,
        budget=budget,
        persist=persist,
    )


def analyze(
    run_id: str,
    *,
    user_intent: Literal["analyze_run", "analyze_step"],
    selected_step: int | None,
    llm_client: AgentLLM | Any | None = None,
    budget: int = INITIAL_BUDGET,
    persist: bool = True,
) -> AgentExecutionResult:
    return _consume_stream(
        stream_analyze(
            run_id,
            user_intent=user_intent,
            selected_step=selected_step,
            llm_client=llm_client,
            budget=budget,
            persist=persist,
        )
    )


def stream_analyze(
    run_id: str,
    *,
    user_intent: Literal["analyze_run", "analyze_step"],
    selected_step: int | None,
    llm_client: AgentLLM | Any | None = None,
    budget: int = INITIAL_BUDGET,
    persist: bool = True,
) -> Iterator[AgentStreamItem]:
    trace = AgentTrace(
        run_id=run_id,
        user_intent=user_intent,
        selected_step=selected_step,
        turn_count=1,
        terminated_by="error",
    )
    state: GraphState = {
        "run_id": run_id,
        "user_intent": user_intent,
        "selected_step": selected_step,
        "trajectory_digest": [],
        "messages": [],
        "tool_call_count": 0,
        "eval_case_draft": None,
        "errors": [],
        "trace": trace,
        "turn": 0,
        "budget": budget,
        "per_turn_budgeted_calls": 0,
        "pending_tool_calls": [],
        "active_tool_call": None,
        "llm_client": llm_client,
        "done": False,
    }
    result: AgentExecutionResult | None = None
    start = time.perf_counter()
    try:
        # Always surface the preprocess phase so the UI can render a row
        # that the user can expand to view the per-step digest. On a cold
        # start the row spins for ~30s while the per-step low-detail VLM
        # runs; on a cache hit the row appears already-done with a
        # different message so the user still sees "the digest existed"
        # instead of jumping straight to the first tool call.
        cache_hit = not _needs_preprocess(run_id)
        run = storage.load_run(run_id)
        step_count = len(run.steps) if run else 0
        _append_event(
            trace,
            "phase",
            turn=0,
            name="preprocess",
            args={"step_count": step_count, "cached": cache_hit},
            message=(
                "Loaded cached trajectory digest"
                if cache_hit
                else "Building trajectory digest"
            ),
        )
        yield trace.events[-1]
        # Pre-streamed events (currently the optional preprocess phase) must
        # not be re-yielded by _stream_graph_result. start_seq stays 0 so the
        # phase event is still counted in new_events for the final result.
        result = yield from _stream_graph_result(
            state,
            start_seq=0,
            include_preprocess=True,
            emitted_seq=len(trace.events),
        )
    except Exception as exc:
        _record_graph_execution_error(state, trace=trace, turn=0, error=str(exc))
        yield trace.events[-1]
        raise
    finally:
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        final_trace = result.trace if result is not None else trace
        final_trace.runtime_ms += elapsed_ms
        _current_turn_metrics(final_trace, 0).runtime_ms += elapsed_ms
        if persist:
            storage.save_trace(run_id, final_trace)
    yield AgentStreamDone(result)


def _needs_preprocess(run_id: str) -> bool:
    """Return True if the digest cache is missing or stale for the active VLM.

    Mirrors the freshness check inside ``preprocess.load_or_build_digest``
    so the streaming layer can warn the user *before* the (potentially
    30s+) per-step VLM loop runs. False means the digest will be served
    from cache and no phase event needs to be emitted.
    """

    from backend.app.llm import get_vlm_client
    from backend.app.preprocess import PREPROCESS_VERSION

    cached = storage.load_digest(run_id)
    if cached is None:
        return True
    client = get_vlm_client()
    return not (
        cached.preprocess_version == PREPROCESS_VERSION
        and cached.preprocess_model == client.model_name
    )


def followup(
    run_id: str,
    message: str,
    *,
    llm_client: AgentLLM | Any | None = None,
    budget: int = FOLLOWUP_BUDGET,
    persist: bool = True,
) -> AgentExecutionResult:
    return _consume_stream(
        stream_followup(
            run_id,
            message,
            llm_client=llm_client,
            budget=budget,
            persist=persist,
        )
    )


def stream_followup(
    run_id: str,
    message: str,
    *,
    llm_client: AgentLLM | Any | None = None,
    budget: int = FOLLOWUP_BUDGET,
    persist: bool = True,
) -> Iterator[AgentStreamItem]:
    trace = storage.load_trace(run_id)
    if trace is None:
        raise NoPriorTraceError(f"no prior trace for run_id: {run_id}")

    turn = trace.turn_count
    start_seq = len(trace.events)
    _append_event(trace, "user_message", turn=turn, message=message)
    yield trace.events[-1]

    result: AgentExecutionResult | None = None
    state: GraphState | None = None
    start = time.perf_counter()
    try:
        digest = storage.load_digest(run_id) or preprocess.load_or_build_digest(run_id)
        state = {
            "run_id": run_id,
            "user_intent": trace.user_intent,
            "selected_step": trace.selected_step,
            "trajectory_digest": [step.model_dump(mode="json") for step in digest.steps],
            "messages": _messages_from_trace(trace),
            "tool_call_count": trace.tool_call_count,
            "eval_case_draft": _latest_eval_case_draft(trace),
            "errors": [],
            "trace": trace,
            "turn": turn,
            "budget": budget,
            "per_turn_budgeted_calls": 0,
            "pending_tool_calls": [],
            "active_tool_call": None,
            "llm_client": llm_client,
            "done": False,
        }
        result = yield from _stream_graph_result(
            state,
            start_seq=start_seq,
            include_preprocess=False,
            emitted_seq=len(trace.events),
        )
        result.trace.turn_count = max(result.trace.turn_count, turn + 1)
        result.new_events = result.trace.events[start_seq:]
    except Exception as exc:
        _record_graph_execution_error(state, trace=trace, turn=turn, error=str(exc))
        trace.turn_count = max(trace.turn_count, turn + 1)
        yield trace.events[-1]
        raise
    finally:
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        final_trace = result.trace if result is not None else trace
        final_trace.runtime_ms += elapsed_ms
        _current_turn_metrics(final_trace, turn).runtime_ms += elapsed_ms
        if persist:
            storage.save_trace(run_id, final_trace)
    yield AgentStreamDone(result)


def _consume_stream(stream: Iterator[AgentStreamItem]) -> AgentExecutionResult:
    result: AgentExecutionResult | None = None
    for item in stream:
        if isinstance(item, AgentStreamDone):
            result = item.result
    if result is None:
        raise RuntimeError("agent stream ended without a terminal result")
    return result


def _stream_graph_result(
    state: GraphState,
    *,
    start_seq: int,
    include_preprocess: bool,
    emitted_seq: int | None = None,
) -> Iterator[AgentStreamItem]:
    emitted_seq = start_seq if emitted_seq is None else emitted_seq
    final_state = state
    for kind, payload in _run_graph_stream(state, include_preprocess=include_preprocess):
        if kind == "snapshot":
            final_state = payload
            trace = final_state["trace"]
            while emitted_seq < len(trace.events):
                yield trace.events[emitted_seq]
                emitted_seq += 1
        elif kind == "delta":
            # payload is (AIMessageChunk, metadata) from LangGraph's
            # stream_mode="messages". Surface only the text delta; the
            # AIMessage with the merged content + tool_calls still
            # arrives via the next snapshot (LangGraph aggregates it
            # back into state["messages"]).
            delta = _build_agent_delta(payload, final_state)
            if delta is not None:
                yield delta

    trace = final_state["trace"]
    while emitted_seq < len(trace.events):
        yield trace.events[emitted_seq]
        emitted_seq += 1
    return _execution_result(trace, final_state, start_seq)


def _build_agent_delta(payload: Any, state: GraphState) -> AgentDelta | None:
    """Extract a wire-friendly AgentDelta from a (chunk, metadata) tuple.

    Returns None when the chunk carries no streamable text (tool-call
    only chunks, empty content, malformed payloads). The agent_message
    that eventually lands in the trace is the authoritative copy; we
    deliberately don't try to also expose tool-call streaming for
    now — tool args can't be acted on until complete anyway.
    """

    if not isinstance(payload, tuple) or len(payload) < 1:
        return None
    chunk = payload[0]
    content = getattr(chunk, "content", "")
    if not isinstance(content, str) or not content:
        return None
    stream_id = getattr(chunk, "id", None)
    if not isinstance(stream_id, str) or not stream_id:
        # Without a stable id we can't reliably group deltas of one
        # message on the client — skip rather than emit ambiguous frames.
        return None
    return AgentDelta(turn=int(state.get("turn", 0) or 0), text=content, stream_id=stream_id)


def _run_graph_stream(
    state: GraphState, *, include_preprocess: bool
) -> Iterator[tuple[str, Any]]:
    """Tagged graph stream.

    Yields ``("snapshot", GraphState)`` for full-state updates (used to
    extract new trace events) and ``("delta", (AIMessageChunk, metadata))``
    for LLM token chunks emitted during a node call. Streaming requires
    LangGraph + an LLM client with streaming=True; the fallback path
    never emits "delta" tuples.

    The tagged shape replaces the original "yield raw GraphState"
    interface so callers can dispatch the two kinds of events without
    inspecting payload types.
    """

    if StateGraph is None:
        for snapshot in _fallback_graph_stream(state, include_preprocess=include_preprocess):
            yield ("snapshot", snapshot)
        return

    graph = _compiled_graph(include_preprocess)
    # stream_mode=["values", "messages"] is multi-mode: each yield is
    # (mode, payload). "values" → full state snapshot (same as before);
    # "messages" → (AIMessageChunk, metadata) tuple for an LLM token
    # delta. The latter only fires when the active client has
    # streaming=True; mocks never produce these.
    for mode, payload in graph.stream(
        state,
        stream_mode=["values", "messages"],
        config={"recursion_limit": _graph_recursion_limit(state["budget"])},
    ):
        if mode == "values":
            if isinstance(payload, dict) and "trace" in payload:
                yield ("snapshot", payload)
        elif mode == "messages":
            yield ("delta", payload)


@lru_cache(maxsize=2)
def _compiled_graph(include_preprocess: bool) -> Any:
    if StateGraph is None:  # pragma: no cover - guarded by caller
        raise RuntimeError("langgraph is not installed")

    graph = StateGraph(GraphState)
    graph.add_node("agent", _agent_node)
    graph.add_node("tool_call", _tool_call_node)
    graph.add_node("execute_tool", _execute_tool_node)

    if include_preprocess:
        graph.add_node("preprocess", preprocess_node)
        graph.add_edge(START, "preprocess")
        graph.add_edge("preprocess", "agent")
    else:
        graph.add_edge(START, "agent")

    graph.add_conditional_edges(
        "agent",
        _after_agent_node,
        {"tool_call": "tool_call", END: END},
    )
    graph.add_edge("tool_call", "execute_tool")
    graph.add_conditional_edges(
        "execute_tool",
        _after_execute_tool_node,
        {"tool_call": "tool_call", "agent": "agent", END: END},
    )
    return graph.compile()


def _fallback_graph_stream(state: GraphState, *, include_preprocess: bool) -> Iterator[GraphState]:
    if include_preprocess:
        state = preprocess_node(state)  # type: ignore[assignment]
        yield state

    steps = 0
    limit = _graph_recursion_limit(state["budget"])
    while True:
        steps = _advance_graph_step(steps, limit)
        state = _agent_node(state)
        yield state
        if _after_agent_node(state) == END:
            return
        while _after_execute_tool_node(state) == "tool_call":
            steps = _advance_graph_step(steps, limit)
            state = _tool_call_node(state)
            yield state
            steps = _advance_graph_step(steps, limit)
            state = _execute_tool_node(state)
            yield state
            if _after_execute_tool_node(state) == END:
                return


def _graph_recursion_limit(budget: int) -> int:
    return max(25, (budget + 8) * 6)


def _advance_graph_step(steps: int, limit: int) -> int:
    if steps >= limit:
        raise RuntimeError("agent graph exceeded recursion limit")
    return steps + 1


def _agent_node(state: GraphState) -> GraphState:
    client = state.get("llm_client")
    if client is None:
        client = _default_llm_client(state)
        state["llm_client"] = client
    message = _invoke_model(client, state["messages"])
    state["messages"].append(message)
    _accumulate_token_usage(state["trace"], message, turn=state["turn"])
    # Only record an agent_message event when the model produced actual text.
    # When the model returns only tool_calls (content == ""), the tool_call
    # events that follow already represent the agent's intent — emitting a
    # blank agent_message just renders as "(empty message)" in the UI.
    content = _message_content(message)
    if content.strip():
        _append_event(state["trace"], "agent_message", turn=state["turn"], message=content)

    try:
        tool_calls = _extract_tool_calls(message)
    except (TypeError, ValueError) as exc:
        error = f"invalid tool call from agent: {exc}"
        state["errors"].append(error)
        state["trace"].terminated_by = "error"
        state["eval_case_draft"] = None
        state["pending_tool_calls"] = []
        state["active_tool_call"] = None
        state["done"] = True
        _append_event(state["trace"], "tool_error", turn=state["turn"], error=error)
        return state
    state["pending_tool_calls"] = tool_calls
    state["active_tool_call"] = None
    if tool_calls:
        state["done"] = False
        return state

    # No tool calls. Behavior depends on which turn we're in:
    #
    # * Initial analyze (turn == 0): the agent MUST end by calling
    #   propose_eval_case. Stopping with plain text is an error — flip
    #   terminated_by=error, wipe the (non-existent) draft, record the
    #   diagnostic event so the UI surfaces it.
    #
    # * Followup turn (turn > 0): the followup system prompt explicitly
    #   allows the agent to answer clarification questions in plain text
    #   without invoking any tool ("If the user only asks a clarification
    #   question, answer in plain text without invoking any tool."). That
    #   case is a legitimate turn termination: the agent_message event
    #   already records the answer the user sees, the previous turn's
    #   draft remains valid, and terminated_by should keep reflecting the
    #   prior verdict (typically "propose_eval_case"). Just mark the turn
    #   done and exit — do not touch errors, eval_case_draft, or
    #   terminated_by. (Previously this branch fired on every turn and
    #   silently destroyed the user's draft as soon as they asked a
    #   followup question that didn't require new tool calls.)
    if state["turn"] > 0:
        state["done"] = True
        return state

    error = "agent stopped without calling propose_eval_case"
    state["errors"].append(error)
    state["trace"].terminated_by = "error"
    state["eval_case_draft"] = None
    state["done"] = True
    _append_event(state["trace"], "tool_error", turn=state["turn"], error=error)
    return state


def _tool_call_node(state: GraphState) -> GraphState:
    pending = list(state.get("pending_tool_calls") or [])
    if not pending:
        state["active_tool_call"] = None
        state["done"] = True
        return state

    tool_call = pending.pop(0)
    state["pending_tool_calls"] = pending
    state["active_tool_call"] = tool_call
    _append_event(
        state["trace"],
        "tool_call",
        turn=state["turn"],
        name=tool_call["name"],
        args=_sanitize_for_trace(tool_call["args"]),
    )
    return state


def _execute_tool_node(state: GraphState) -> GraphState:
    tool_call = state.get("active_tool_call")
    if tool_call is None:
        state["done"] = True
        return state

    trace = state["trace"]
    turn = state["turn"]
    name = tool_call["name"]
    args = _sanitize_for_trace(tool_call["args"])
    call_id = tool_call["id"]
    state["active_tool_call"] = None

    if name in BUDGETED_TOOLS:
        if state["per_turn_budgeted_calls"] >= state["budget"]:
            error = f"tool-call budget exceeded before {name}; budget={state['budget']}"
            state["errors"].append(error)
            _append_event(trace, "tool_error", turn=turn, name=name, args=args, error=error)
            trace.terminated_by = "budget_exceeded"
            state["eval_case_draft"] = None
            state["done"] = True
            return state
        state["per_turn_budgeted_calls"] += 1
        trace.tool_call_count += 1
        state["tool_call_count"] = trace.tool_call_count

    if name not in _TOOL_REGISTRY:
        _record_recoverable_tool_error(state, name=name, args=args, call_id=call_id, error=f"unknown tool: {name}")
        return state

    if name == TERMINAL_TOOL:
        error = _proposal_context_error(args, trace)
        if error is not None:
            _record_terminal_tool_error(state, name=name, args=args, error=error)
            return state

    try:
        result = _TOOL_REGISTRY[name](**args)
    except Exception as exc:
        error = str(exc)
        if name == TERMINAL_TOOL:
            _record_terminal_tool_error(state, name=name, args=args, error=error)
        else:
            _record_recoverable_tool_error(state, name=name, args=args, call_id=call_id, error=error)
        return state

    if isinstance(result, dict) and isinstance(result.get("tool_error"), str):
        error = result["tool_error"]
        if name == TERMINAL_TOOL:
            _record_terminal_tool_error(state, name=name, args=args, error=error)
        else:
            _record_recoverable_tool_error(state, name=name, args=args, call_id=call_id, error=error)
        return state

    event_result = _trace_result_payload(result)
    _append_event(trace, "tool_result", turn=turn, name=name, result=event_result)
    _append_tool_message(state, name=name, call_id=call_id, payload=event_result)

    if name == TERMINAL_TOOL:
        state["eval_case_draft"] = event_result
        trace.terminated_by = "propose_eval_case"
        state["done"] = True
    return state


def _after_agent_node(state: GraphState) -> str:
    if state.get("done") or not state.get("pending_tool_calls"):
        return END
    return "tool_call"


def _after_execute_tool_node(state: GraphState) -> str:
    if state.get("done"):
        return END
    if state.get("pending_tool_calls"):
        return "tool_call"
    return "agent"


def _record_recoverable_tool_error(
    state: GraphState,
    *,
    name: str,
    args: dict[str, Any],
    call_id: str,
    error: str,
) -> None:
    _append_event(state["trace"], "tool_error", turn=state["turn"], name=name, args=args, error=error)
    _append_tool_message(state, name=name, call_id=call_id, payload={"tool_error": error})


def _record_terminal_tool_error(
    state: GraphState,
    *,
    name: str,
    args: dict[str, Any],
    error: str,
) -> None:
    state["errors"].append(error)
    _append_event(state["trace"], "tool_error", turn=state["turn"], name=name, args=args, error=error)
    state["trace"].terminated_by = "error"
    state["eval_case_draft"] = None
    state["done"] = True


def _record_graph_execution_error(
    state: GraphState | None,
    *,
    trace: AgentTrace,
    turn: int,
    error: str,
) -> None:
    if state is not None:
        state["errors"].append(error)
        state["eval_case_draft"] = None
        state["done"] = True
    trace.terminated_by = "error"
    _append_event(trace, "tool_error", turn=turn, name="graph_execution", error=error)


def _append_tool_message(state: GraphState, *, name: str, call_id: str, payload: dict[str, Any]) -> None:
    state["messages"].append(
        ToolMessage(
            content=json.dumps(payload, ensure_ascii=False, sort_keys=True),
            name=name,
            tool_call_id=call_id,
        )
    )


def _execution_result(trace: AgentTrace, state: EvalState, start_seq: int) -> AgentExecutionResult:
    return AgentExecutionResult(
        trace=trace,
        eval_case_draft=state["eval_case_draft"] if trace.terminated_by == "propose_eval_case" else None,
        new_events=trace.events[start_seq:],
        errors=list(state["errors"]),
    )


def _accumulate_token_usage(trace: AgentTrace, message: Any, *, turn: int) -> None:
    """Pull ``usage_metadata`` off an AIMessage and add it to the trace.

    Real OpenAI calls populate ``AIMessage.usage_metadata`` as
    ``{input_tokens, output_tokens, total_tokens}``. Offline mocks and
    the fallback message classes don't have the attribute — the
    ``getattr`` default keeps the call site loop-safe so we silently
    record 0 for those turns instead of crashing.

    Writes to BOTH the cumulative ``AgentTrace.input_tokens`` /
    ``output_tokens`` and the per-turn entry in ``trace.turn_metrics``.
    The UI reads per-turn; SPEC.md cost ablation reads cumulative.
    """

    usage = getattr(message, "usage_metadata", None)
    if not isinstance(usage, dict):
        return
    try:
        input_delta = int(usage.get("input_tokens", 0) or 0)
        output_delta = int(usage.get("output_tokens", 0) or 0)
    except (TypeError, ValueError):
        # A model returning non-numeric token counts shouldn't take down
        # the whole agent loop — just skip the increment.
        return
    trace.input_tokens += input_delta
    trace.output_tokens += output_delta
    metrics = _current_turn_metrics(trace, turn)
    metrics.input_tokens += input_delta
    metrics.output_tokens += output_delta


def _current_turn_metrics(trace: AgentTrace, turn: int) -> TurnMetrics:
    """Return the ``TurnMetrics`` for ``turn``, creating it if absent.

    Used by both the token accumulator and the wall-clock writer in
    ``stream_analyze`` / ``stream_followup`` so each turn's counters
    live in one place that the UI can render verbatim.
    """

    for entry in trace.turn_metrics:
        if entry.turn == turn:
            return entry
    entry = TurnMetrics(turn=turn)
    trace.turn_metrics.append(entry)
    return entry


def _invoke_model(client: AgentLLM | Any, messages: list[AnyMessage]) -> AnyMessage:
    if hasattr(client, "invoke"):
        return client.invoke(messages)
    if callable(client):
        return client(messages)
    raise TypeError("llm_client must expose invoke(messages) or be callable")


def _default_llm_client(state: EvalState) -> AgentLLM:
    model_name = os.environ.get("TRAJECTA_AGENT_MODEL")
    api_key = os.environ.get("OPENAI_API_KEY")
    if model_name and api_key:
        try:  # pragma: no cover - production-only path
            from langchain_openai import ChatOpenAI
        except ImportError:
            pass
        else:
            # streaming=True makes .invoke() internally stream + aggregate
            # AND emit chunk callbacks. LangGraph's stream_mode="messages"
            # listens to those callbacks, so the node code keeps using
            # blocking .invoke() while the streaming layer surfaces deltas.
            # stream_usage / include_usage on the request preserves
            # token accumulation at end-of-stream (without it, the final
            # AIMessage.usage_metadata is None on streamed calls).
            model = ChatOpenAI(
                model=model_name,
                temperature=0,
                streaming=True,
                stream_usage=True,
            )
            if hasattr(model, "bind_tools"):
                return model.bind_tools(list(_TOOL_REGISTRY.values()))
            return model
    return OfflineAgentMock(state)


class OfflineAgentMock:
    """Deterministic no-network agent used when no production LLM is configured."""

    def __init__(self, state: EvalState) -> None:
        self._state = state
        self._stage = 0

    def invoke(self, messages: list[AnyMessage]) -> AnyMessage:
        run_id = self._state["run_id"]
        if self._stage == 0:
            self._stage += 1
            return _ai_tool_call("get_run", {"run_id": run_id})
        if self._stage == 1:
            self._stage += 1
            step_index = self._selected_failure_step()
            return _ai_tool_call(
                "get_step_detail",
                {"run_id": run_id, "step_index": step_index, "image_detail": "high"},
            )
        if self._stage == 2:
            self._stage += 1
            task = storage.load_run(run_id).task
            return _ai_tool_call(
                "find_similar_successful_run",
                {"task": task, "top_k": 1, "exclude_run_id": run_id},
            )
        if self._stage == 3:
            successful = _last_tool_items(messages, "find_similar_successful_run")
            self._stage += 1
            if successful:
                return _ai_tool_call("get_run", {"run_id": successful[0]["run_id"]})
            return _ai_tool_call("search_failure_memory", {"query": "missed_constraint", "top_k": 1})
        if self._stage == 4:
            self._stage += 1
            if _last_tool_items(messages, "find_similar_successful_run"):
                return _ai_tool_call("search_failure_memory", {"query": "missed_constraint", "top_k": 1})
            return self._proposal_message(messages)
        self._stage += 1
        return self._proposal_message(messages)

    def _selected_failure_step(self) -> int:
        # All step indices are 1-based (aligned with source step keys and
        # screenshot filenames). If neither a user-selected step nor any
        # failed step is available, fall back to the first digest step
        # rather than the invalid sentinel 0.
        if self._state["user_intent"] == "analyze_step" and self._state["selected_step"] is not None:
            return self._state["selected_step"]
        for step in self._state["trajectory_digest"]:
            if step.get("result_status") == "failed":
                return int(step.get("index", 1))
        first = self._state["trajectory_digest"][0] if self._state["trajectory_digest"] else None
        return int(first.get("index", 1)) if isinstance(first, dict) else 1

    def _proposal_message(self, messages: list[AnyMessage]) -> AnyMessage:
        run_id = self._state["run_id"]
        failure_step = self._selected_failure_step()
        memory_items = _last_tool_items(messages, "search_failure_memory")
        retrieved_ids = [memory_items[0]["case_id"]] if memory_items else []
        evidence: list[dict[str, Any]] = [
            {
                "claim": f"Step {failure_step} was inspected as the failure region.",
                "source": "step_detail_high",
                "run_id": run_id,
                "step_index": failure_step,
            }
        ]
        if retrieved_ids:
            evidence.append(
                {
                    "claim": "A retrieved memory describes a missed constraint failure pattern.",
                    "source": "failure_memory",
                    "context_id": retrieved_ids[0],
                }
            )
        return _ai_tool_call(
            TERMINAL_TOOL,
            {
                "run_id": run_id,
                "failure_step": failure_step,
                "failure_type": "missed_constraint",
                "expected_behavior": "The agent should satisfy the user's stated constraint before finishing.",
                "actual_behavior": "The inspected trajectory does not show reliable evidence that the constraint was satisfied.",
                "evidence": evidence,
                "regression_rule": "Verify the task constraint is satisfied before marking the browser task complete.",
                "retrieved_context_ids": retrieved_ids,
            },
        )


def _ai_tool_call(name: str, args: dict[str, Any]) -> AnyMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call_{name}"}])


def _initial_messages(state: EvalState, *, followup: bool) -> list[AnyMessage]:
    return [
        SystemMessage(content=_system_prompt(followup=followup)),
        HumanMessage(
            content=json.dumps(
                {
                    "run_id": state["run_id"],
                    "user_intent": state["user_intent"],
                    "selected_step": state["selected_step"],
                    "trajectory_digest": state["trajectory_digest"],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        ),
    ]


def _system_prompt(*, followup: bool) -> str:
    # Deliberately minimal. Not prompt-tuning — only the contract minimum
    # (propose_eval_case is the terminal tool; never invent evidence).
    # Real prompt engineering is deferred.
    common = (
        "You are Trajecta's Eval Agent. Use the declared tools only. "
        "The first HumanMessage carries `run_id` and the full "
        "`trajectory_digest`. All step indices are 1-based and match the "
        "source dataset's step keys and screenshot filenames (e.g., "
        "step_index=7 ↔ screenshot_007.png). Survey the digest to "
        "identify suspicious steps, deep-inspect them with "
        "`get_step_detail` at high detail, retrieve relevant failure "
        "memory and prior eval cases when they would inform your "
        "verdict, and finish by calling `propose_eval_case` "
        "(success-shape if no failure found). Never fabricate evidence; "
        "mark unavailable evidence explicitly via `source=\"unavailable\"`. "
        "OPTIONAL: pass `suggested_followups` (max 4) on the terminal "
        "call — short {label, message} pairs the user can click to ask "
        "a useful next question grounded in THIS trace (e.g., 'Inspect "
        "step 4 in detail', 'Compare with successful run X'). Skip if "
        "no specific next step is obvious."
    )
    if followup:
        return (
            "You are Trajecta's Eval Agent resuming a previous analysis. "
            "Use targeted tool calls and call `propose_eval_case` only "
            "when revising the eval case draft. If the user only asks a "
            "clarification question, answer in plain text without "
            "invoking any tool."
        )
    return common


def _messages_from_trace(trace: AgentTrace) -> list[AnyMessage]:
    messages: list[AnyMessage] = [
        SystemMessage(content=_system_prompt(followup=True)),
        HumanMessage(
            content=json.dumps(
                {
                    "run_id": trace.run_id,
                    "user_intent": trace.user_intent,
                    "selected_step": trace.selected_step,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        ),
    ]
    last_tool_call_id: str | None = None
    for event in trace.events:
        if event.type == "user_message" and event.message:
            messages.append(HumanMessage(content=event.message))
        elif event.type == "agent_message" and event.message:
            messages.append(AIMessage(content=event.message or ""))
        elif event.type == "tool_call" and event.name:
            last_tool_call_id = f"trace_{event.seq}"
            messages.append(
                AIMessage(
                    content="",
                    tool_calls=[{"name": event.name, "args": event.args or {}, "id": last_tool_call_id}],
                )
            )
        elif event.type == "tool_result":
            messages.append(
                ToolMessage(
                    content=json.dumps(event.result or {}, ensure_ascii=False, sort_keys=True),
                    name=event.name,
                    tool_call_id=last_tool_call_id or f"trace_{event.seq}",
                )
            )
        elif event.type == "tool_error":
            # Turn-level diagnostics (e.g. "agent stopped without calling
            # propose_eval_case", "agent graph exceeded recursion limit",
            # "invalid tool call from agent") are recorded as tool_error
            # events with no name + no preceding tool_call. Replaying them
            # as ToolMessage produces an orphan that OpenAI rejects with
            # "messages with role 'tool' must be a response to a preceeding
            # message with 'tool_calls'". Skip them; the trace still keeps
            # them for the UI / observability.
            if not event.name:
                continue
            messages.append(
                ToolMessage(
                    content=json.dumps({"tool_error": event.error or ""}, ensure_ascii=False, sort_keys=True),
                    name=event.name,
                    tool_call_id=last_tool_call_id or f"trace_{event.seq}",
                )
            )
    return messages


def _append_event(
    trace: AgentTrace,
    event_type: Literal["agent_message", "user_message", "tool_call", "tool_result", "tool_error"],
    *,
    turn: int,
    name: str | None = None,
    args: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    message: str | None = None,
    error: str | None = None,
) -> AgentTraceEvent:
    event = AgentTraceEvent(
        seq=len(trace.events),
        type=event_type,
        name=name,
        args=_sanitize_for_trace(args) if args is not None else None,
        result=_sanitize_for_trace(result) if result is not None else None,
        message=message,
        error=error,
        turn=turn,
    )
    trace.events.append(event)
    return event


def _extract_tool_calls(message: AnyMessage) -> list[dict[str, Any]]:
    raw_calls = getattr(message, "tool_calls", None)
    if not raw_calls:
        additional = getattr(message, "additional_kwargs", {}) or {}
        raw_calls = additional.get("tool_calls") or []
    return [_normalize_tool_call(raw, index) for index, raw in enumerate(raw_calls)]


def _normalize_tool_call(raw: Any, index: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise TypeError(f"unsupported tool call shape at index {index}: {raw!r}")

    call_id = str(raw.get("id") or f"call_{index}")
    if "name" in raw:
        raw_name = raw.get("name")
        args = raw.get("args") or raw.get("arguments") or {}
    else:
        function = raw.get("function") or {}
        if not isinstance(function, dict):
            raise TypeError(f"tool call function at index {index} must be a dict")
        raw_name = function.get("name")
        args = function.get("arguments") or {}
    if not isinstance(raw_name, str) or not raw_name.strip():
        raise ValueError(f"tool call at index {index} is missing a tool name")
    name = raw_name.strip()
    if isinstance(args, str):
        args = json.loads(args) if args else {}
    if not isinstance(args, dict):
        raise TypeError(f"tool call args for {name!r} must be a dict")
    return {"id": call_id, "name": name, "args": args}


def _message_content(message: AnyMessage) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    return json.dumps(_sanitize_for_trace(content), ensure_ascii=False, sort_keys=True)


def _trace_result_payload(result: Any) -> dict[str, Any]:
    sanitized = _sanitize_for_trace(result)
    if isinstance(sanitized, dict):
        return sanitized
    return {"items": sanitized}


def _sanitize_for_trace(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _sanitize_for_trace(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {
            str(key): _sanitize_for_trace(item)
            for key, item in value.items()
            if str(key) not in _SENSITIVE_RESULT_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize_for_trace(item) for item in value]
    if isinstance(value, bytes):
        return "<bytes omitted>"
    return value


def _proposal_context_error(args: dict[str, Any], trace: AgentTrace) -> str | None:
    available = _retrieved_case_ids(trace)

    # docs/eval_agent.md L234: every case_id in retrieved_context_ids must
    # appear in some search_* tool_result of the same trace.
    requested = set(args.get("retrieved_context_ids") or [])
    missing = sorted(context_id for context_id in requested if context_id not in available)
    if missing:
        # Common agent mistake: passing run_ids from find_similar_successful_run
        # into retrieved_context_ids. That tool's results aren't case_ids and
        # are explicitly excluded by docs/contracts.md L332. Detect this case
        # and tell the agent specifically what to drop on its retry — a
        # generic "not found" message often loops the same mistake.
        run_ids_seen = _retrieved_run_ids(trace)
        misused_run_ids = sorted(ctx for ctx in missing if ctx in run_ids_seen)
        if misused_run_ids:
            return (
                "retrieved_context_ids must contain only case_ids returned by "
                "search_failure_memory or search_eval_cases. The following IDs "
                "are run_ids from find_similar_successful_run and must be "
                "omitted (similar-run comparisons are tracked via the "
                "AgentTrace, not retrieved_context_ids): "
                + ", ".join(misused_run_ids)
            )
        return "retrieved_context_ids not found in prior retrieval tool_result: " + ", ".join(missing)

    # docs/eval_agent.md L235: every EvidenceItem with source in
    # {failure_memory, eval_case} must carry a context_id that appears in
    # a prior retrieval tool result. An evidence item that cites failure
    # memory or a prior eval case without a verifiable context_id is
    # unsupported evidence — fail the terminal call so the draft is never
    # surfaced to the user.
    contextual_sources = {"failure_memory", "eval_case"}
    evidence_unset: list[int] = []
    evidence_unknown: list[str] = []
    for index, item in enumerate(args.get("evidence") or []):
        if not isinstance(item, dict):
            continue
        if item.get("source") not in contextual_sources:
            continue
        context_id = item.get("context_id")
        if not isinstance(context_id, str) or not context_id:
            evidence_unset.append(index)
            continue
        if context_id not in available:
            evidence_unknown.append(context_id)
    if evidence_unset:
        return (
            "evidence with source in {eval_case, failure_memory} requires "
            f"context_id; missing at evidence indices: {evidence_unset}"
        )
    if evidence_unknown:
        return (
            "evidence context_id not found in prior retrieval tool_result: "
            + ", ".join(sorted(set(evidence_unknown)))
        )
    return None


def _retrieved_case_ids(trace: AgentTrace) -> set[str]:
    ids: set[str] = set()
    for event in trace.events:
        if event.type != "tool_result" or event.name not in SEARCH_TOOLS:
            continue
        ids.update(_case_ids_in_payload(event.result or {}))
    return ids


def _retrieved_run_ids(trace: AgentTrace) -> set[str]:
    """Run_ids surfaced by find_similar_successful_run results.

    Used to give the agent a pedagogical error when it confuses run_ids
    (returned by find_similar_successful_run) with case_ids (the only
    legal contents of retrieved_context_ids). NOT used to expand the set
    of legal IDs — run_ids remain ineligible per docs/contracts.md L332.
    """

    ids: set[str] = set()
    for event in trace.events:
        if event.type != "tool_result" or event.name != "find_similar_successful_run":
            continue
        items = (event.result or {}).get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict) and isinstance(item.get("run_id"), str):
                ids.add(item["run_id"])
    return ids


def _case_ids_in_payload(payload: Any) -> set[str]:
    ids: set[str] = set()
    if isinstance(payload, dict):
        case_id = payload.get("case_id")
        if isinstance(case_id, str):
            ids.add(case_id)
        for value in payload.values():
            ids.update(_case_ids_in_payload(value))
    elif isinstance(payload, list):
        for item in payload:
            ids.update(_case_ids_in_payload(item))
    return ids


def _latest_eval_case_draft(trace: AgentTrace) -> dict[str, Any] | None:
    for event in reversed(trace.events):
        if event.type == "tool_result" and event.name == TERMINAL_TOOL:
            return event.result
    return None


def _last_tool_items(messages: list[AnyMessage], name: str) -> list[dict[str, Any]]:
    for message in reversed(messages):
        if getattr(message, "name", None) != name:
            continue
        try:
            payload = json.loads(getattr(message, "content", "") or "{}")
        except json.JSONDecodeError:
            return []
        if isinstance(payload, dict):
            items = payload.get("items")
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
            if "case_id" in payload or "run_id" in payload:
                return [payload]
    return []
