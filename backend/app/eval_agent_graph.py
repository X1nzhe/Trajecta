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

from backend.app import preprocess, prompts, storage, tools
from backend.app.llm import resolve_model_provider, vlm_usage_scope
from backend.app.schemas import AgentTrace, AgentTraceEvent, TurnMetrics

try:  # pragma: no cover - exercised when optional production deps are present
    from langgraph.graph import END, START, StateGraph
except ImportError:  # pragma: no cover - local no-dependency test fallback
    END = "__end__"
    START = "__start__"
    StateGraph = None

try:  # pragma: no cover - exercised when optional production deps are present
    from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
    from langchain_core.messages import messages_from_dict, messages_to_dict
except ImportError:  # pragma: no cover - the fallback is exercised in local tests
    AnyMessage = Any
    # No real LangChain → no opaque replay buffer. _serialize_messages /
    # _restore_messages short-circuit to None, and follow-ups fall back to
    # rebuilding messages from trace events (the offline mock path).
    messages_to_dict = None
    messages_from_dict = None

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
    trajectory_id: str
    user_intent: Literal["analyze_trajectory", "analyze_step"]
    selected_step: int | None
    trajectory_digest: list[dict[str, Any]]
    messages: list[AnyMessage]
    tool_call_count: int
    eval_case_draft: dict[str, Any] | None
    errors: list[str]
    prompt_version: str | None


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
    # Count of recoverable propose_eval_case failures so far this run; bounded by
    # MAX_TERMINAL_RETRIES before the terminal error becomes fatal.
    terminal_error_retries: int
    # Per-run Spotlighting token, minted in stream_analyze / stream_followup
    # and threaded to every node so digest / step-detail wrapping never depends
    # on a ContextVar that the sync NDJSON stream loses across chunks.
    spotlight_token: str | None
    spotlighting_enabled: bool


@dataclass
class AgentExecutionResult:
    trace: AgentTrace
    eval_case_draft: dict[str, Any] | None
    new_events: list[AgentTraceEvent]
    errors: list[str]
    # Opaque LangChain message history (messages_to_dict form) for follow-up
    # replay; None on offline/mock runs or any serialization failure. Persisted
    # off-trace via storage.save_agent_messages — see _serialize_messages.
    raw_messages: list[dict[str, Any]] | None = None


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
    "search_failure_eval_cases",
    "find_similar_successful_trajectory",
}
SEARCH_TOOLS = {"search_failure_memory", "search_failure_eval_cases"}
TERMINAL_TOOL = "propose_eval_case"
INITIAL_BUDGET = 8
# Followup runs the same loop as the initial analyze; giving it the same
# budget lets a single followup do a full re-analysis (e.g. user asks the
# agent to reconsider with a hint, agent re-inspects N steps and revises
# the draft). Was 4 historically — bumped to 8 once we saw real followups
# routinely needing get_step_detail + a fresh search pair.
FOLLOWUP_BUDGET = 8
# A malformed propose_eval_case (schema/validation error, failure_type out of
# vocabulary, or a retrieved_context_ids / evidence-context mismatch) is fed back
# to the model so it can self-correct — the agent often gets the verdict right and
# only mis-shapes one field. Bounded by this many retries per run; past the cap the
# run terminates with terminated_by="error", preserving the invariant "no valid
# EvalCase => error". propose_eval_case is NOT in BUDGETED_TOOLS, so these retries
# don't consume the tool-call budget; the cap + graph recursion limit bound them.
MAX_TERMINAL_RETRIES = 2
_SENSITIVE_RESULT_KEYS = {"screenshot_bytes", "image_bytes", "image_data"}

# Tag stamped on the persisted opaque replay buffer. Bump if the on-disk shape
# changes incompatibly; _restore_messages refuses any other version and the
# caller falls back to rebuilding messages from trace events.
MESSAGES_FORMAT_VERSION = "lc-messages-v1"

_TOOL_REGISTRY = {
    "get_trajectory": tools.get_trajectory,
    "get_step_detail": tools.get_step_detail,
    "search_failure_memory": tools.search_failure_memory,
    "search_failure_eval_cases": tools.search_failure_eval_cases,
    "find_similar_successful_trajectory": tools.find_similar_successful_trajectory,
    "propose_eval_case": tools.propose_eval_case,
}


def preprocess_node(state: GraphState) -> GraphState:
    digest = preprocess.load_or_build_digest(state["trajectory_id"])
    state["trajectory_digest"] = [step.model_dump(mode="json") for step in digest.steps]
    if not state["messages"]:
        state["messages"] = _initial_messages(state, followup=False)
    return state


def analyze_trajectory(
    trajectory_id: str,
    *,
    llm_client: AgentLLM | Any | None = None,
    budget: int = INITIAL_BUDGET,
    persist: bool = True,
    source: Literal["ui", "eval", "mcp"] = "ui",
) -> AgentExecutionResult:
    return _consume_stream(
        stream_analyze_trajectory(
            trajectory_id,
            llm_client=llm_client,
            budget=budget,
            persist=persist,
            source=source,
        )
    )


def stream_analyze_trajectory(
    trajectory_id: str,
    *,
    llm_client: AgentLLM | Any | None = None,
    budget: int = INITIAL_BUDGET,
    persist: bool = True,
    source: Literal["ui", "eval", "mcp"] = "ui",
) -> Iterator[AgentStreamItem]:
    """Analyze the full trajectory.

    There is no per-step entry point. The agent always works against the
    entire trajectory_digest and decides which steps to deep-inspect.
    Failure attribution is the agent's responsibility, surfaced as
    ``EvalCase.failure_step``. New traces always carry
    ``user_intent="analyze_trajectory"`` and ``selected_step=None``; the
    ``selected_step`` field is retained in the schema only for back-compat
    reading of older traces from disk.

    ``source`` records run origin on the trace ("ui" default, "eval" from
    the agent_eval harness, "mcp" from the MCP composite tool).
    """

    yield from stream_analyze(
        trajectory_id,
        user_intent="analyze_trajectory",
        selected_step=None,
        llm_client=llm_client,
        budget=budget,
        persist=persist,
        source=source,
    )


def analyze(
    trajectory_id: str,
    *,
    user_intent: Literal["analyze_trajectory", "analyze_step"],
    selected_step: int | None,
    llm_client: AgentLLM | Any | None = None,
    budget: int = INITIAL_BUDGET,
    persist: bool = True,
    source: Literal["ui", "eval", "mcp"] = "ui",
) -> AgentExecutionResult:
    return _consume_stream(
        stream_analyze(
            trajectory_id,
            user_intent=user_intent,
            selected_step=selected_step,
            llm_client=llm_client,
            budget=budget,
            persist=persist,
            source=source,
        )
    )


def stream_analyze(
    trajectory_id: str,
    *,
    user_intent: Literal["analyze_trajectory", "analyze_step"],
    selected_step: int | None,
    llm_client: AgentLLM | Any | None = None,
    budget: int = INITIAL_BUDGET,
    persist: bool = True,
    source: Literal["ui", "eval", "mcp"] = "ui",
) -> Iterator[AgentStreamItem]:
    # Mirror the provider split used by _default_llm_client: a real agent only
    # runs when TRAJECTA_AGENT_MODEL and that provider's API key are set;
    # otherwise OfflineAgentMock takes over. Stamp "mock" in that case so the
    # frontend can label runs honestly instead of advertising a model that
    # didn't actually answer.
    prompt_bundle = prompts.active_prompt_bundle()
    spotlighting_on = prompts.spotlighting_enabled()
    spotlight_token = prompts.new_spotlight_token()
    prompts.set_spotlight_token(spotlight_token)
    _agent_model_env = os.environ.get("TRAJECTA_AGENT_MODEL")
    _agent_provider = resolve_model_provider(_agent_model_env)
    trace_model = _agent_model_env if (_agent_model_env and _agent_provider.api_key) else "mock"
    # Same gate for the VLM side. Without both model and provider key,
    # get_step_detail goes through MockVLMClient and we stamp "mock" to match
    # how preprocess_model behaves.
    _vlm_model_env = os.environ.get("TRAJECTA_VLM_MODEL")
    _vlm_provider = resolve_model_provider(_vlm_model_env)
    trace_vlm_model = _vlm_model_env if (_vlm_model_env and _vlm_provider.api_key) else "mock"
    trace = AgentTrace(
        trajectory_id=trajectory_id,
        user_intent=user_intent,
        selected_step=selected_step,
        source=source,
        turn_count=1,
        terminated_by="error",
        model=trace_model,
        prompt_version=prompt_bundle.version,
        prompt_sha256=prompt_bundle.sha256,
        spotlighting_enabled=spotlighting_on,
        vlm_model=trace_vlm_model,
    )
    state: GraphState = {
        "trajectory_id": trajectory_id,
        "user_intent": user_intent,
        "selected_step": selected_step,
        "trajectory_digest": [],
        "messages": [],
        "tool_call_count": 0,
        "eval_case_draft": None,
        "errors": [],
        "prompt_version": prompt_bundle.version,
        "trace": trace,
        "turn": 0,
        "budget": budget,
        "per_turn_budgeted_calls": 0,
        "terminal_error_retries": 0,
        "pending_tool_calls": [],
        "active_tool_call": None,
        "llm_client": llm_client,
        "done": False,
        # Carry the token as data, not just in the ContextVar: the sync NDJSON
        # stream is pumped one chunk per thread-pool task (fresh context each),
        # so the set() above is gone by the chunk that runs the graph. Nodes
        # read the token from here when wrapping (see spotlight_wrap).
        "spotlight_token": spotlight_token,
        "spotlighting_enabled": spotlighting_on,
    }
    result: AgentExecutionResult | None = None
    start = time.perf_counter()
    # Capture every VLM call (preprocess pass + any get_step_detail tool
    # calls inside the graph) into one bucket — at the end, we copy the
    # totals onto the trace so the UI can show "this analyze cost N
    # input + M output VLM tokens" without a separate API round-trip.
    with vlm_usage_scope() as vlm_usage:
        try:
            # Always surface the preprocess phase so the UI can render a row
            # that the user can expand to view the per-step digest. On a cold
            # start the row spins for ~30s while the per-step low-detail VLM
            # runs; on a cache hit the row appears already-done with a
            # different message so the user still sees "the digest existed"
            # instead of jumping straight to the first tool call.
            cache_hit = not _needs_preprocess(trajectory_id)
            trajectory = storage.load_trajectory(trajectory_id)
            step_count = len(trajectory.steps) if trajectory else 0
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
            # First scope on this trace — assign rather than add, since
            # the trace started with vlm_*_tokens=0.
            final_trace.vlm_input_tokens = vlm_usage["input"]
            final_trace.vlm_output_tokens = vlm_usage["output"]
            if persist:
                storage.save_trace(trajectory_id, final_trace)
                _persist_replay_buffer(trajectory_id, result)
    yield AgentStreamDone(result)


def _needs_preprocess(trajectory_id: str) -> bool:
    """Return True if the digest cache is missing or stale for the active VLM.

    Mirrors the freshness check inside ``preprocess.load_or_build_digest``
    so the streaming layer can warn the user *before* the (potentially
    30s+) per-step VLM loop runs. False means the digest will be served
    from cache and no phase event needs to be emitted.
    """

    from backend.app.llm import get_vlm_client
    from backend.app.preprocess import PREPROCESS_VERSION

    cached = storage.load_digest(trajectory_id)
    if cached is None:
        return True
    client = get_vlm_client()
    return not (
        cached.preprocess_version == PREPROCESS_VERSION
        and cached.preprocess_model == client.model_name
    )


def followup(
    trajectory_id: str,
    message: str,
    *,
    llm_client: AgentLLM | Any | None = None,
    budget: int = FOLLOWUP_BUDGET,
    persist: bool = True,
) -> AgentExecutionResult:
    return _consume_stream(
        stream_followup(
            trajectory_id,
            message,
            llm_client=llm_client,
            budget=budget,
            persist=persist,
        )
    )


def stream_followup(
    trajectory_id: str,
    message: str,
    *,
    llm_client: AgentLLM | Any | None = None,
    budget: int = FOLLOWUP_BUDGET,
    persist: bool = True,
) -> Iterator[AgentStreamItem]:
    trace = storage.load_trace(trajectory_id)
    if trace is None:
        raise NoPriorTraceError(f"no prior trace for trajectory_id: {trajectory_id}")

    turn = trace.turn_count
    start_seq = len(trace.events)
    _append_event(trace, "user_message", turn=turn, message=message)
    yield trace.events[-1]

    result: AgentExecutionResult | None = None
    state: GraphState | None = None
    start = time.perf_counter()
    # Re-mint a Spotlighting token for this followup turn. Wraps inside
    # the new turn use the new token; messages replayed from the saved
    # trace keep their original token bytes. Both delimiter shapes match
    # the `<TRAJECTA_DATA_*>` preamble pattern so the defense still applies.
    spotlight_token = prompts.new_spotlight_token()
    prompts.set_spotlight_token(spotlight_token)
    # Followup VLM scope: any get_step_detail tool call in this turn adds
    # to the bucket; at the end we ADD to the trace's cumulative counters
    # (not assign — earlier turns already contributed).
    with vlm_usage_scope() as vlm_usage:
        try:
            digest = storage.load_digest(trajectory_id) or preprocess.load_or_build_digest(trajectory_id)
            # Prefer replaying the opaque message buffer (provider metadata such
            # as Gemini's thought_signature intact); fall back to lossy
            # reconstruction from trace events for old/undecodable buffers and
            # offline runs. The restore branch must append the new user turn
            # itself — the fallback gets it free from the user_message event
            # already appended above.
            restored = _restore_messages(storage.load_agent_messages(trajectory_id), trace)
            if restored is not None:
                restored.append(HumanMessage(content=message))
                followup_messages = restored
            else:
                followup_messages = _messages_from_trace(trace)
            state = {
                "trajectory_id": trajectory_id,
                "user_intent": trace.user_intent,
                "selected_step": trace.selected_step,
                "trajectory_digest": [step.model_dump(mode="json") for step in digest.steps],
                "messages": followup_messages,
                "tool_call_count": trace.tool_call_count,
                "eval_case_draft": _latest_eval_case_draft(trace),
                "errors": [],
                "prompt_version": trace.prompt_version,
                "trace": trace,
                "turn": turn,
                "budget": budget,
                "per_turn_budgeted_calls": 0,
                "terminal_error_retries": 0,
                "pending_tool_calls": [],
                "active_tool_call": None,
                "llm_client": llm_client,
                "done": False,
                # See stream_analyze: the token rides in state so each node can
                # wrap with it after the stream's cross-chunk context copy.
                "spotlight_token": spotlight_token,
                "spotlighting_enabled": trace.spotlighting_enabled,
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
            final_trace.vlm_input_tokens += vlm_usage["input"]
            final_trace.vlm_output_tokens += vlm_usage["output"]
            if persist:
                storage.save_trace(trajectory_id, final_trace)
                _persist_replay_buffer(trajectory_id, result)
    yield AgentStreamDone(result)


def _persist_replay_buffer(trajectory_id: str, result: AgentExecutionResult | None) -> None:
    """Save the opaque follow-up replay buffer alongside the trace.

    No-op when the run produced no serializable messages (offline mock /
    LangChain absent / serialization failure) or when the stream ended without a
    result (exception path) — the next follow-up then reconstructs from trace
    events. ``storage.save_agent_messages`` swallows a missing-table
    ``OperationalError``, so this stays best-effort.
    """

    if result is None or result.raw_messages is None:
        return
    storage.save_agent_messages(
        trajectory_id,
        {"format_version": MESSAGES_FORMAT_VERSION, "messages": result.raw_messages},
    )


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

    Critically: LangGraph's stream_mode="messages" yields chunks for
    EVERY message touched by a node — including SystemMessage and
    HumanMessage that the node loads into state["messages"]. Without
    a type filter the system prompt and the initial HumanMessage
    (which carries trajectory_id + trajectory_digest) would leak verbatim to
    the wire. Restrict to AIMessageChunk so only LLM-generated tokens
    cross the boundary.
    """

    if not isinstance(payload, tuple) or len(payload) < 1:
        return None
    chunk = payload[0]
    # Duck-typed check — LangChain core's AIMessageChunk.type is the
    # literal string "AIMessageChunk". Final aggregated AIMessage has
    # type "ai" and is filtered out here too (its content arrives via
    # the agent_message trace event, no need to duplicate as a delta).
    chunk_type = getattr(chunk, "type", None)
    if chunk_type != "AIMessageChunk":
        return None
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
        {"tool_call": "tool_call", "agent": "agent", END: END},
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
        next_after_agent = _after_agent_node(state)
        if next_after_agent == END:
            return
        if next_after_agent == "agent":
            # _nudge_agent_retry queued a corrective HumanMessage; loop
            # straight back to agent_node without running any tools.
            continue
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
    # When the model returns only tool_calls (content == "" or content == []),
    # the tool_call events that follow already represent the agent's intent —
    # emitting a blank agent_message just renders as "(empty message)" in the UI.
    content = _message_content(message)
    if content.strip():
        _append_event(state["trace"], "agent_message", turn=state["turn"], message=content)

    try:
        tool_calls = _extract_tool_calls(message)
    except (TypeError, ValueError) as exc:
        # Agent-side bug: the tool_calls payload is malformed (missing
        # name, args not a dict, etc.). Recoverable — feed the
        # diagnostic back as a HumanMessage hint and let the agent
        # try again on the next graph iteration.
        _nudge_agent_retry(
            state,
            error=f"invalid tool call from agent: {exc}",
            hint=(
                "The previous response's tool_calls payload was malformed. "
                "Each tool call must include a non-empty `name` and an `args` "
                "object matching that tool's schema. Try again with a valid "
                "tool call."
            ),
        )
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

    # Initial analyze stopped without calling propose_eval_case.
    # Recoverable: the agent might have produced an explanatory text
    # reply by mistake; nudge it to actually invoke the terminal tool.
    # LangGraph's recursion limit eventually surfaces a real graph
    # execution error if the agent never recovers.
    _nudge_agent_retry(
        state,
        error="agent stopped without calling propose_eval_case",
        hint=(
            "You must terminate the initial analysis by calling "
            "`propose_eval_case` (success-shape if no failure was found). "
            "A plain-text reply with no tool call is not a valid "
            "termination for turn 0. Please call propose_eval_case now."
        ),
    )
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
            _record_terminal_tool_error_or_retry(state, name=name, args=args, call_id=call_id, error=error)
            return state

    dispatch_args = args
    if name == "search_failure_memory":
        # Server-side leakage guard: force the current trajectory_id as
        # exclude_source_trajectory_id regardless of what the LLM emitted (or didn't).
        # See docs/testing.md "Failure-memory retrieval leakage". The trace
        # event recorded upstream still reflects the LLM-emitted args; this
        # only mutates dispatch.
        dispatch_args = {**args, "exclude_source_trajectory_id": trace.trajectory_id}
    elif name == "search_failure_eval_cases":
        # Same leakage guard for prior EvalCases: an EvalCase derived from
        # the run currently under analysis carries that run's verdict, so
        # retrieving it would be direct answer leakage. Force the source-run
        # filter regardless of LLM-emitted args, mirroring search_failure_memory.
        dispatch_args = {**args, "exclude_source_trajectory_id": trace.trajectory_id}
    elif name == "find_similar_successful_trajectory":
        # Same leakage guard for the replay-and-diff retrieval path. The prompt
        # instructs the agent to pass exclude_trajectory_id=current_trajectory_id, but the
        # tool signature defaults it to None and there is no schema-level
        # requirement that the LLM include it. If the LLM forgets, a run that
        # is itself in the successful_trajectories collection (e.g. a
        # golden-set sample whose human_validated success EvalCase was
        # previously promoted)
        # would re-surface as "similar to itself" — direct leakage of the
        # success verdict. Force the injection here so the guarantee holds
        # structurally rather than by prompt-following compliance.
        dispatch_args = {**args, "exclude_trajectory_id": trace.trajectory_id}

    try:
        result = _TOOL_REGISTRY[name](**dispatch_args)
    except Exception as exc:
        error = str(exc)
        if name == TERMINAL_TOOL:
            _record_terminal_tool_error_or_retry(state, name=name, args=args, call_id=call_id, error=error)
        else:
            _record_recoverable_tool_error(state, name=name, args=args, call_id=call_id, error=error)
        return state

    if isinstance(result, dict) and isinstance(result.get("tool_error"), str):
        error = result["tool_error"]
        if name == TERMINAL_TOOL:
            _record_terminal_tool_error_or_retry(state, name=name, args=args, call_id=call_id, error=error)
        else:
            _record_recoverable_tool_error(state, name=name, args=args, call_id=call_id, error=error)
        return state

    # Phase 8 B6 Spotlighting: wrap untrusted text in the get_step_detail
    # result here, at the agent-tool-result seam, not inside the tool. The
    # tool stays reusable by the token-free HTTP detail endpoint + MCP read
    # tool; wrapping only happens when the result enters the Eval Agent's own
    # LLM context, where stream_analyze has already set a per-run token.
    if name == "get_step_detail" and isinstance(result, dict):
        result = _spotlight_wrap_step_detail(result, state.get("spotlight_token"))

    event_result = _trace_result_payload(result)
    _append_event(trace, "tool_result", turn=turn, name=name, result=event_result)
    _append_tool_message(state, name=name, call_id=call_id, payload=event_result)

    if name == TERMINAL_TOOL:
        state["eval_case_draft"] = event_result
        trace.terminated_by = "propose_eval_case"
        state["done"] = True
    return state


def _after_agent_node(state: GraphState) -> str:
    if state.get("done"):
        return END
    if state.get("pending_tool_calls"):
        return "tool_call"
    # No pending tool calls and not done — _nudge_agent_retry queued a
    # corrective HumanMessage for the agent. Loop back to agent_node
    # for another LLM call. Bounded by the graph's recursion_limit.
    return "agent"


def _after_execute_tool_node(state: GraphState) -> str:
    if state.get("done"):
        return END
    if state.get("pending_tool_calls"):
        return "tool_call"
    return "agent"


def _nudge_agent_retry(state: GraphState, *, error: str, hint: str) -> None:
    """Recover from an agent-side mistake by feeding back a corrective hint.

    Used when the LLM produced output we can't act on (no tool calls
    when one was required, malformed tool_calls payload, etc.).
    Instead of flipping terminated_by="error" and surfacing a "Tool
    error" in the UI, we:
      - record the diagnostic as a tool_error trace event so the
        observability surface still sees it,
      - append a HumanMessage with the error + a one-line hint
        explaining what to do differently next,
      - leave done=False so _after_agent_node routes us back to the
        agent for another attempt.

    LangGraph's recursion_limit (5x budget by default) bounds the
    retry loop; if the agent keeps misbehaving we'll surface a
    legitimate graph-execution error via _record_graph_execution_error.
    """

    trace = state["trace"]
    _append_event(trace, "tool_error", turn=state["turn"], error=error)
    state["errors"].append(error)
    state["messages"].append(HumanMessage(content=f"{error}\n\n{hint}"))
    state["pending_tool_calls"] = []
    state["active_tool_call"] = None
    state["done"] = False


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


def _record_terminal_tool_error_or_retry(
    state: GraphState,
    *,
    name: str,
    args: dict[str, Any],
    call_id: str,
    error: str,
) -> None:
    """Feed a malformed propose_eval_case back to the agent for a bounded retry.

    The agent frequently gets the verdict right and only mis-shapes one field
    (e.g. an EvidenceItem with the assertion under ``text`` instead of ``claim``,
    or a step list crammed into the ``source`` enum). The precise validation error
    is highly correctable, so for the first ``MAX_TERMINAL_RETRIES`` failures we
    record the diagnostic and return it as a ToolMessage — exactly like
    ``_record_recoverable_tool_error`` for non-terminal tools — leaving ``done``
    False so ``_after_execute_tool_node`` routes back to the agent for another
    propose_eval_case. Past the cap we fall back to ``_record_terminal_tool_error``
    (hard ``terminated_by="error"``), so a persistently-bad agent still terminates
    cleanly with the real error. Recoverable retries do not append to
    ``state["errors"]`` (mirroring the non-terminal path); only the final fatal
    error does.
    """

    if state.get("terminal_error_retries", 0) >= MAX_TERMINAL_RETRIES:
        _record_terminal_tool_error(state, name=name, args=args, error=error)
        return
    state["terminal_error_retries"] = state.get("terminal_error_retries", 0) + 1
    _append_event(state["trace"], "tool_error", turn=state["turn"], name=name, args=args, error=error)
    hint = (
        "Fix the arguments and call propose_eval_case again. Each evidence item "
        "must be {\"claim\": <assertion text>, \"source\": <one of the allowed "
        "enum values>, ...}; put step numbers in the integer `step_index`, never "
        "in `source`."
    )
    _append_tool_message(
        state, name=name, call_id=call_id, payload={"tool_error": f"{error}\n\n{hint}"}
    )


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
        raw_messages=_serialize_messages(state["messages"]),
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
    The UI reads per-turn; PROJECT.md cost ablation reads cumulative.
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
    provider = resolve_model_provider(model_name)
    if model_name and provider.api_key:
        if provider.provider == "gemini":
            # Gemini 3.x thinking models require thought_signature to be
            # preserved across multi-turn tool-calling loops. The OpenAI-
            # compatible endpoint + ChatOpenAI silently drops the signature,
            # causing 400 errors on the second turn. ChatGoogleGenerativeAI
            # speaks the native Gemini protocol and round-trips the signature —
            # but ONLY at langchain-google-genai >= 3.0 (we pin >= 4, which also
            # adds a DUMMY_THOUGHT_SIGNATURE fallback for parts lacking one).
            # The 2.x line has zero thought_signature support and still 400s.
            # That version floor is why requirements.txt is on the LangChain 1.x
            # stack (langchain-core >= 1.4); see AGENTS.md "LLM / VLM Configuration".
            try:  # pragma: no cover - production-only path
                from langchain_google_genai import ChatGoogleGenerativeAI
            except ImportError:
                pass
            else:
                model = ChatGoogleGenerativeAI(
                    model=model_name,
                    google_api_key=provider.api_key,
                    temperature=0,
                    streaming=True,
                )
                if hasattr(model, "bind_tools"):
                    return model.bind_tools(list(_TOOL_REGISTRY.values()))
                return model
        else:
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
                model_kwargs = {
                    "model": model_name,
                    "api_key": provider.api_key,
                    "temperature": 0,
                    "streaming": True,
                    "stream_usage": True,
                }
                if provider.base_url is not None:
                    model_kwargs["base_url"] = provider.base_url
                model = ChatOpenAI(**model_kwargs)
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
        trajectory_id = self._state["trajectory_id"]
        if self._stage == 0:
            self._stage += 1
            return _ai_tool_call("get_trajectory", {"trajectory_id": trajectory_id})
        if self._stage == 1:
            self._stage += 1
            step_index = self._selected_failure_step()
            return _ai_tool_call(
                "get_step_detail",
                {"trajectory_id": trajectory_id, "step_index": step_index, "image_detail": "high"},
            )
        if self._stage == 2:
            self._stage += 1
            task = storage.load_trajectory(trajectory_id).task
            return _ai_tool_call(
                "find_similar_successful_trajectory",
                {"task": task, "top_k": 1, "exclude_trajectory_id": trajectory_id},
            )
        if self._stage == 3:
            successful = _last_tool_items(messages, "find_similar_successful_trajectory")
            self._stage += 1
            if successful:
                return _ai_tool_call("get_trajectory", {"trajectory_id": successful[0]["trajectory_id"]})
            return _ai_tool_call("search_failure_memory", {"query": "missed_constraint", "top_k": 1})
        if self._stage == 4:
            self._stage += 1
            if _last_tool_items(messages, "find_similar_successful_trajectory"):
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
        trajectory_id = self._state["trajectory_id"]
        failure_step = self._selected_failure_step()
        memory_items = _last_tool_items(messages, "search_failure_memory")
        retrieved_ids = [memory_items[0]["case_id"]] if memory_items else []
        evidence: list[dict[str, Any]] = [
            {
                "claim": f"Step {failure_step} was inspected as the failure region.",
                "source": "step_detail_high",
                "trajectory_id": trajectory_id,
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
                "trajectory_id": trajectory_id,
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


# Untrusted text fields in each StepDigest row (see backend/app/schemas.py
# StepDigest). Structural enums and indices are intentionally not wrapped —
# they are not attacker-influenced text and wrapping them adds JSON bloat
# with no defense value.
_DIGEST_UNTRUSTED_TEXT_FIELDS: tuple[str, ...] = (
    "action_text",
    "action_target",
    "url",
    "title",
    "vlm_low_detail_summary",
)


def _wrap_digest_for_prompt(
    rows: list[dict[str, Any]], token: str | None = None
) -> list[dict[str, Any]]:
    """Return a copy of ``rows`` with untrusted text fields Spotlight-wrapped.

    Walks every digest row (already serialised to plain dicts by
    ``preprocess_node``) and replaces each field listed in
    ``_DIGEST_UNTRUSTED_TEXT_FIELDS`` with its wrapped form. Non-text
    fields (``index``, ``action_type``, ``result_status``,
    ``coord_validation_status``, ``has_screenshot``) pass through. None /
    empty values pass through too (see ``spotlight_wrap_optional``).
    Off-mode degrades to identity automatically.
    """

    wrapped: list[dict[str, Any]] = []
    for row in rows:
        new_row = dict(row)
        for field in _DIGEST_UNTRUSTED_TEXT_FIELDS:
            if field in new_row:
                new_row[field] = prompts.spotlight_wrap_optional(
                    new_row[field], token
                )
        wrapped.append(new_row)
    return wrapped


def _spotlight_wrap_step_detail(
    result: dict[str, Any], token: str | None = None
) -> dict[str, Any]:
    """Spotlight-wrap untrusted text in a ``get_step_detail`` result.

    Applied at the agent tool-result seam (not inside ``tools.get_step_detail``)
    so the tool stays reusable by the token-free HTTP detail endpoint and MCP
    read tool. ``task_context.task`` is the user's own goal and stays unwrapped;
    structural fields (ids, statuses, coords, screenshot_url) are not text and
    pass through. Mutates the freshly-built result dict in place. Off-mode and
    None/empty values degrade to identity via ``spotlight_wrap_optional``.
    """

    def wrap(text: str | None) -> str | None:
        return prompts.spotlight_wrap_optional(text, token)

    if "vlm_summary" in result:
        result["vlm_summary"] = wrap(result["vlm_summary"])
    task_context = result.get("task_context")
    if isinstance(task_context, dict):
        for field in ("url", "title", "action_label", "action_text", "action_raw"):
            if field in task_context:
                task_context[field] = wrap(task_context[field])
    observation = result.get("observation")
    if isinstance(observation, dict):
        for field in ("url", "title", "visible_text"):
            if field in observation:
                observation[field] = wrap(observation[field])
    action = result.get("action")
    if isinstance(action, dict):
        for field in ("label", "text", "raw"):
            if field in action:
                action[field] = wrap(action[field])
    return result


def _initial_messages(state: GraphState, *, followup: bool) -> list[AnyMessage]:
    return [
        SystemMessage(
            content=_system_prompt(
                followup=followup,
                prompt_version=state.get("prompt_version"),
            )
        ),
        HumanMessage(
            content=json.dumps(
                {
                    "trajectory_id": state["trajectory_id"],
                    "user_intent": state["user_intent"],
                    "selected_step": state["selected_step"],
                    "trajectory_digest": _wrap_digest_for_prompt(
                        state["trajectory_digest"],
                        state.get("spotlight_token"),
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        ),
    ]


def _system_prompt(*, followup: bool, prompt_version: str | None = None) -> str:
    bundle = prompts.load_prompt_bundle(prompt_version)
    return bundle.followup if followup else bundle.system


def _messages_from_trace(trace: AgentTrace) -> list[AnyMessage]:
    messages: list[AnyMessage] = [
        SystemMessage(
            content=_system_prompt(
                followup=True,
                prompt_version=trace.prompt_version,
            )
        ),
        HumanMessage(
            content=json.dumps(
                {
                    "trajectory_id": trace.trajectory_id,
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


def _serialize_messages(messages: list[AnyMessage]) -> list[dict[str, Any]] | None:
    """Serialize the live LangChain message list for follow-up replay.

    Returns the ``messages_to_dict`` form, which preserves provider-private
    metadata (``additional_kwargs`` / ``response_metadata`` — where e.g. Gemini
    thinking models' ``thought_signature`` rides) that the trace-event
    projection in ``_messages_from_trace`` drops. Returns ``None`` when LangChain
    isn't installed (the offline mock path uses ``_FallbackMessage`` shims that
    ``messages_to_dict`` can't serialize) or on any serialization error, so
    persistence stays best-effort and never aborts the run.
    """

    if messages_to_dict is None:
        return None
    try:
        return messages_to_dict(messages)
    except Exception:  # pragma: no cover - defensive against shims / odd shapes
        return None


def _restore_messages(
    payload: dict[str, Any] | None, trace: AgentTrace
) -> list[AnyMessage] | None:
    """Rebuild the message list from a persisted replay buffer, or ``None``.

    ``None`` (caller falls back to ``_messages_from_trace``) when the buffer is
    absent, tagged with an unknown ``format_version``, LangChain is unavailable,
    or ``messages_from_dict`` fails. On success the leading ``SystemMessage`` is
    swapped for the follow-up system prompt — the buffer stored the initial
    turn's system prompt, and follow-ups use a different one (mirrors
    ``_messages_from_trace``, which builds with ``followup=True``).
    """

    if not isinstance(payload, dict) or messages_from_dict is None:
        return None
    if payload.get("format_version") != MESSAGES_FORMAT_VERSION:
        return None
    raw = payload.get("messages")
    if not isinstance(raw, list):
        return None
    try:
        messages = messages_from_dict(raw)
    except Exception:  # pragma: no cover - defensive against format drift
        return None
    if not messages:
        return None
    followup_system = SystemMessage(
        content=_system_prompt(followup=True, prompt_version=trace.prompt_version)
    )
    if isinstance(messages[0], SystemMessage):
        messages[0] = followup_system
    else:
        messages.insert(0, followup_system)
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
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        # Gemini / LangChain often return content=[] on tool-only turns.
        # json.dumps([]) == "[]", which previously leaked into agent_message
        # events and rendered as literal brackets in the UI.
        if not content:
            return ""
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                if block:
                    parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
            else:
                return json.dumps(_sanitize_for_trace(content), ensure_ascii=False, sort_keys=True)
        return "".join(parts)
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
        # Common agent mistake: passing trajectory_ids from find_similar_successful_trajectory
        # into retrieved_context_ids. That tool's results aren't case_ids and
        # are explicitly excluded by docs/contracts.md L332. Detect this case
        # and tell the agent specifically what to drop on its retry — a
        # generic "not found" message often loops the same mistake.
        trajectory_ids_seen = _retrieved_trajectory_ids(trace)
        misused_trajectory_ids = sorted(ctx for ctx in missing if ctx in trajectory_ids_seen)
        if misused_trajectory_ids:
            return (
                "retrieved_context_ids must contain only case_ids returned by "
                "search_failure_memory or search_failure_eval_cases. The following IDs "
                "are trajectory_ids from find_similar_successful_trajectory and must be "
                "omitted (similar-run comparisons are tracked via the "
                "AgentTrace, not retrieved_context_ids): "
                + ", ".join(misused_trajectory_ids)
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


def _retrieved_trajectory_ids(trace: AgentTrace) -> set[str]:
    """Run_ids surfaced by find_similar_successful_trajectory results.

    Used to give the agent a pedagogical error when it confuses trajectory_ids
    (returned by find_similar_successful_trajectory) with case_ids (the only
    legal contents of retrieved_context_ids). NOT used to expand the set
    of legal IDs — trajectory_ids remain ineligible per docs/contracts.md L332.
    """

    ids: set[str] = set()
    for event in trace.events:
        if event.type != "tool_result" or event.name != "find_similar_successful_trajectory":
            continue
        items = (event.result or {}).get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict) and isinstance(item.get("trajectory_id"), str):
                ids.add(item["trajectory_id"])
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
            if "case_id" in payload or "trajectory_id" in payload:
                return [payload]
    return []
