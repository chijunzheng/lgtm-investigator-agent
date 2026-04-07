"""Core agent loop: the reasoning engine that investigates incidents.

This module implements the "think → act → observe" loop:
  1. Send conversation history to the LLM
  2. LLM either returns tool calls (act) or a final diagnosis (done)
  3. Execute tool calls against Loki/Mimir/Tempo
  4. Add results to context → back to step 1

All version differences (V1→V4) are controlled by boolean feature flags,
not separate implementations. The eval framework compares versions by
instantiating Agent with different flag combinations.

Architecture:
  Agent.run()                  — main entry point (user message → diagnosis)
  Agent._call_with_retry()     — streaming OpenAI API call with retry logic
  Agent._execute_tools_*()     — sequential or parallel tool execution
  _accumulate_tool_call()      — reconstructs tool calls from streaming deltas
  _extract_diagnosis()         — extracts the diagnosis block from LLM output
  _format_diagnosis()          — adds markdown formatting for CLI display
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor
import openai
from openai import NOT_GIVEN
from rich.console import Console
from rich.markdown import Markdown
from context import ContextManager
from tools_registry import TOOL_DEFINITIONS, dispatch
from cost_tracker import CostTracker
from config import MODEL, SWEEP_MODEL, MAX_AGENT_ITERATIONS, MAX_TOOL_CALLS_PER_TURN, MAX_RETRIES

_console = Console()

# Fields that appear in the structured diagnosis output (defined in system_v2 prompt).
# Used by _format_diagnosis() to add markdown headers for CLI readability.
_DIAGNOSIS_FIELDS = [
    "Root Cause:", "Confidence:", "Evidence:", "Contradictions:",
    "Contradicting Evidence:", "Not Investigated:", "Remediation:",
]


def _extract_diagnosis(text: str) -> str:
    """Extract the diagnosis block from the final LLM response.

    Strips any preamble before 'Root Cause:' and deduplicates if the
    LLM repeated the diagnosis block.
    """
    if not text:
        return text

    # Find the first occurrence of the diagnosis start marker
    marker = "Root Cause:"
    idx = text.find(marker)
    if idx > 0:
        text = text[idx:]

    # Deduplicate: if the diagnosis block appears twice, keep only the first
    second = text.find(marker, len(marker))
    if second > 0:
        text = text[:second].rstrip()

    return text


def _format_diagnosis(text: str) -> str:
    """Add markdown structure to diagnosis output for readability."""
    # Strip ```diagnosis fences if present
    text = text.replace("```diagnosis", "").replace("```", "")

    # Turn diagnosis fields into markdown headers
    for field in _DIAGNOSIS_FIELDS:
        text = text.replace(field, f"\n### {field}")

    # Ensure bullet points have line breaks before them
    lines = text.split("\n")
    formatted = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("- ") and formatted and formatted[-1].strip():
            formatted.append("")  # blank line before bullet list items
        formatted.append(line)

    return "\n".join(formatted)


class Agent:
    """The investigation agent — takes a symptom, reasons about it, and produces a diagnosis.

    Feature flags (set per version):
      tools_enabled:              Allow tool calls (True for all versions)
      context_management_enabled: Enable micro-compact (V3/V4) — clears old tool results
      tool_metadata_headers:      Add result counts/context to tool output (V2+)
      error_enrichment:           Add fix suggestions when tools fail (V2+)
      parallel_tool_calls:        Execute multiple tool calls concurrently (V2+)
      inject_topology:            Auto-inject service list before first turn (V2+)
      model_routing:              Use cheap model for first call, full model after (V4)
      on_event:                   Callback for streaming events to the Grafana plugin
    """

    def __init__(self, client: openai.OpenAI, system_prompt: str,
                 tools_enabled: bool = True,
                 context_management_enabled: bool = False,
                 tool_metadata_headers: bool = False,
                 error_enrichment: bool = False,
                 parallel_tool_calls: bool = False,
                 inject_topology: bool = False,
                 model_routing: bool = False,
                 on_event: callable = None):
        # -- Configuration (immutable after init) --
        self.client = client
        self.system_prompt = system_prompt
        self.tools_enabled = tools_enabled
        self.context_management_enabled = context_management_enabled
        self.tool_metadata_headers = tool_metadata_headers
        self.error_enrichment = error_enrichment
        self.parallel_tool_calls = parallel_tool_calls
        self.inject_topology = inject_topology
        self.model_routing = model_routing
        self.on_event = on_event

        # -- Mutable state (reset between investigations) --
        self._topology_injected = False  # Only inject topology once per agent lifetime
        self.ctx = ContextManager()      # Conversation history + context optimization
        self.cost = CostTracker()        # Cumulative token usage and cost
        self.total_tool_calls = 0        # Counter for eval metrics
        self.total_llm_calls = 0         # Counter for eval metrics
        self.trace = []                  # Full trace of reasoning + tool calls (for eval)

    def _emit(self, event: dict):
        """Emit an event to the callback if registered."""
        if self.on_event:
            self.on_event(event)

    def run(self, user_input: str, time_window: dict = None) -> str:
        """Main entry point: takes a symptom description, returns the full LLM response.

        The agent loop repeats until the LLM stops calling tools (= it's ready
        to give its diagnosis) or MAX_AGENT_ITERATIONS is reached.

        Args:
            user_input:  The incident symptom (e.g., "Checkout is failing")
            time_window: Optional dict with 'start' and 'end' ISO8601 timestamps
                         to scope all queries to a specific failure period.
        """
        # Append time window to the user message so the LLM scopes its queries
        if time_window:
            user_input += (
                f"\n\n[System context — do NOT mention this to the user. "
                f"Investigation window: {time_window['start']} "
                f"to {time_window['end']}. "
                f"Scope ALL queries to this window.]"
            )

        # Inject topology before first turn (V2/V3)
        if self.inject_topology and not self._topology_injected:
            from tools import list_services
            topology = list_services.execute(metadata_headers=self.tool_metadata_headers)
            self.ctx.add_system_context(
                f"[System topology — injected automatically]\n{topology}"
            )
            self._topology_injected = True
            _console.print("  [dim][topology] injected service list[/dim]")
            self._emit({"type": "topology", "content": topology[:500]})

        self.ctx.add_user_message(user_input)
        self._turn_start_llm_calls = self.total_llm_calls
        full_response = ""
        final_diagnosis = ""

        while self.total_llm_calls < MAX_AGENT_ITERATIONS:
            # -- 1. Context management (V3/V4) --
            # Runs before every LLM call. Keeps last N tool results,
            # clears older ones. This reduces context size per call
            # and removes noise that degrades reasoning.
            if self.context_management_enabled:
                self.ctx.prepare()

            # -- 2. Call OpenAI API with retry --
            # Model routing (V4): use cheap model for the first LLM call
            # per user turn (the parallel sweep). Subsequent calls use the
            # full model for reasoning, correlation, and diagnosis.
            is_first_call_ever = (self.total_llm_calls == 0)
            if self.model_routing and is_first_call_ever:
                current_model = SWEEP_MODEL
                _console.print(f"  [dim][routing] using {SWEEP_MODEL} for sweep[/dim]")
            else:
                current_model = MODEL

            self.total_llm_calls += 1
            api_kwargs = {
                "model": current_model,
                "messages": [
                    {"role": "system", "content": self.system_prompt},
                    *self.ctx.get_messages(),
                ],
                "temperature": 0.1,
                "stream": True,
                "stream_options": {"include_usage": True},
            }
            if self.tools_enabled:
                api_kwargs["tools"] = TOOL_DEFINITIONS
            else:
                api_kwargs["tools"] = NOT_GIVEN

            response = self._call_with_retry(**api_kwargs)
            if response is None:
                break

            message, usage = response

            # -- 3. Track cost --
            if usage:
                self.cost.record(usage, model=current_model)

            # -- 4. Store assistant message --
            self.ctx.add_assistant_message(message)

            # -- 5. Capture text content --
            if message.get("content"):
                full_response += message["content"]
                self.trace.append({
                    "type": "reasoning",
                    "content": message["content"][:500],
                    "timestamp": time.time(),
                })
                self._emit({"type": "reasoning", "content": message["content"]})

            # -- 6. If no tool calls -> done (this content is the final diagnosis) --
            tool_calls = message.get("tool_calls", [])
            if not tool_calls:
                final_diagnosis = _extract_diagnosis(message.get("content") or "")
                break

            # -- 7. Execute tool calls -> append results -> continue loop --
            # Must respond to ALL tool_calls (API requires it), but only
            # execute up to MAX_TOOL_CALLS_PER_TURN; skip the rest.
            executable = tool_calls[:MAX_TOOL_CALLS_PER_TURN]
            overflow = tool_calls[MAX_TOOL_CALLS_PER_TURN:]

            for call in overflow:
                name = call["function"]["name"]
                self.ctx.add_tool_result(call["id"], f"[{name}] skipped -- too many parallel calls", name)

            # Print execution plan, then tool list
            if self.parallel_tool_calls and len(executable) > 1:
                _console.print(f"  [dim][parallel] executing {len(executable)} tool calls concurrently[/dim]")

            for call in executable:
                name = call["function"]["name"]
                args_str = call["function"]["arguments"]
                args_preview = args_str[:80] + "..." if len(args_str) > 80 else args_str
                _console.print(f"  [dim][tool] {name}({args_preview})[/dim]")
                self._emit({"type": "tool_start", "tool": name, "args": args_preview})

            if self.parallel_tool_calls and len(executable) > 1:
                self._execute_tools_parallel(executable)
            else:
                self._execute_tools_sequential(executable)

            if self.context_management_enabled:
                self._print_status()

            print()  # space after tool calls, before next thinking

        self._emit({
            "type": "done",
            "content": final_diagnosis,
            "trace": self.trace,
            "stats": self.get_stats(),
        })
        return full_response

    # -- Tool execution ------------------------------------------------------

    def _execute_one_tool(self, call: dict) -> tuple[str, str, str, str, bool, float]:
        """Execute a single tool call, optionally using the dedup cache (V3/V4)."""
        name = call["function"]["name"]
        args_str = call["function"]["arguments"]
        was_cached = False

        t0 = time.time()
        if self.context_management_enabled:
            result, was_cached = self.ctx.get_or_execute(
                name, args_str,
                lambda n=name, a=args_str: dispatch(n, a, self.tool_metadata_headers, self.error_enrichment),
            )
            if was_cached:
                _console.print(f"  [dim][cache] {name} hit -- reusing cached result[/dim]")
        else:
            result = dispatch(name, args_str, self.tool_metadata_headers, self.error_enrichment)
        duration = round(time.time() - t0, 2)

        return call["id"], name, args_str, result, was_cached, duration

    def _record_tool_result(self, call_id: str, name: str, args_str: str, result: str, was_cached: bool, duration: float = 0.0):
        """Store tool result in context (for LLM) and trace (for eval/frontend)."""
        args_preview = args_str[:80] + "..." if len(args_str) > 80 else args_str
        self.ctx.add_tool_result(call_id, result, name, args_preview=args_preview)
        self.total_tool_calls += 1

        is_error = result.startswith(f"[{name}] ERROR") or result.startswith("Tool error")

        # Extract the query string from args for the frontend panel builder
        query_str = ""
        try:
            args = json.loads(args_str) if args_str else {}
            query_str = args.get("query", args.get("q", ""))
            # For trace tools: build TraceQL from service_name/trace_id args
            if not query_str and name == "query_traces":
                if args.get("service_name"):
                    query_str = '{resource.service.name = "' + args["service_name"] + '"}'
                elif args.get("trace_id"):
                    query_str = args["trace_id"]
        except (json.JSONDecodeError, AttributeError):
            pass

        self.trace.append({
            "type": "tool_call",
            "tool": name,
            "args": args_str[:300],
            "query": query_str,
            "result_preview": result[:300],
            "result_length": len(result),
            "cached": was_cached,
            "error": is_error,
            "duration": duration,
            "timestamp": time.time(),
        })
        self._emit({
            "type": "tool_result",
            "tool": name,
            "args": args_str[:300],
            "query": query_str,
            "duration": duration,
            "cached": was_cached,
            "error": is_error,
            "result_preview": result[:500],
        })

    def _execute_tools_sequential(self, calls: list):
        """Execute tool calls one at a time."""
        for call in calls:
            call_id, name, args_str, result, was_cached, duration = self._execute_one_tool(call)
            self._record_tool_result(call_id, name, args_str, result, was_cached, duration)

    def _execute_tools_parallel(self, calls: list):
        """Execute tool calls concurrently via ThreadPoolExecutor."""
        with ThreadPoolExecutor(max_workers=len(calls)) as pool:
            futures = [pool.submit(self._execute_one_tool, call) for call in calls]
            results = [f.result() for f in futures]
        # Record in original order (preserves message ordering for API)
        for call_id, name, args_str, result, was_cached, duration in results:
            self._record_tool_result(call_id, name, args_str, result, was_cached, duration)

    # -- OpenAI API call with streaming --------------------------------------

    def _call_with_retry(self, **kwargs):
        """Call OpenAI with streaming + exponential backoff retry.

        Returns (message_dict, usage) on success, or None after max retries.
        The message_dict is in OpenAI format: {role, content, tool_calls}.

        Streaming is used so we can render the LLM's reasoning in real-time.
        Tool calls arrive as deltas (partial chunks) that must be accumulated
        into complete calls via _accumulate_tool_call().
        """
        for attempt in range(MAX_RETRIES):
            try:
                stream = self.client.chat.completions.create(**kwargs)

                collected_content = ""
                collected_tool_calls = []
                usage = None

                for chunk in stream:
                    if not chunk.choices:
                        if chunk.usage:
                            usage = chunk.usage
                        continue

                    delta = chunk.choices[0].delta

                    if delta.content:
                        collected_content += delta.content

                    if delta.tool_calls:
                        for tc in delta.tool_calls:
                            _accumulate_tool_call(collected_tool_calls, tc)

                    if chunk.usage:
                        usage = chunk.usage

                if collected_content:
                    if collected_tool_calls:
                        # Thinking before tool calls — render inline
                        print()
                        _console.print(Markdown(collected_content))
                        print()
                    else:
                        # Final answer — render with separator
                        print()
                        _console.rule("[bold]Diagnosis[/bold]", style="green")
                        print()
                        formatted = _format_diagnosis(collected_content)
                        _console.print(Markdown(formatted))
                        print()

                message = {"role": "assistant", "content": collected_content or None}
                if collected_tool_calls:
                    message["tool_calls"] = collected_tool_calls

                return message, usage

            except openai.RateLimitError:
                wait = min(2 ** attempt * 5, 30)
                print(f"  [retry] rate limited, waiting {wait}s...")
                time.sleep(wait)
            except openai.APITimeoutError:
                print(f"  [retry] timeout, attempt {attempt + 1}/{MAX_RETRIES}")
            except openai.APIError as e:
                print(f"  [error] API error: {e}")
                break

        print("  [error] max retries exceeded")
        return None

    def _print_status(self):
        ctx_tokens = self.ctx.estimate_tokens()
        ctx_pct = (ctx_tokens / 1_000_000) * 100
        _console.print(
            f"  [dim][status] ${self.cost.estimated_cost():.4f} | "
            f"{self.cost.total_tokens():,} tok | "
            f"{self.total_tool_calls} calls | "
            f"ctx: {ctx_tokens/1000:.1f}k ({ctx_pct:.1f}%)[/dim]"
        )

    # -- Stats and lifecycle --------------------------------------------------

    def get_stats(self) -> dict:
        """Return all metrics for eval scoring and frontend display."""
        return {
            "total_tool_calls": self.total_tool_calls,
            "total_llm_calls": self.total_llm_calls,
            "context_tokens": self.ctx.estimate_tokens(),
            "cache_hits": self.ctx.cache_hit_count,
            "micro_compacted": self.ctx.micro_compact_count,
            "cost": self.cost.to_dict(),
            "trace": self.trace,
        }

    def reset(self):
        """Clear all state for a new investigation (used by CLI 'reset' command and demos)."""
        self.ctx = ContextManager()
        self.cost = CostTracker()
        self.total_tool_calls = 0
        self.total_llm_calls = 0
        self.trace = []
        self._topology_injected = False


def _accumulate_tool_call(collected: list, delta_tc):
    """Accumulate streamed tool call deltas into complete tool calls.

    OpenAI sends tool calls in fragments during streaming. Each chunk has an
    index and partial data (a piece of the function name or arguments JSON).
    This function reconstructs complete tool calls by appending fragments
    to the correct index position.

    Example stream: delta(idx=0, name="query") → delta(idx=0, name="_logs")
                    → delta(idx=0, args='{"qu') → delta(idx=0, args='ery":...')
    Result:         collected[0] = {name: "query_logs", arguments: '{"query":...'}
    """
    idx = delta_tc.index
    while len(collected) <= idx:
        collected.append({"id": "", "type": "function",
                          "function": {"name": "", "arguments": ""}})
    if delta_tc.id:
        collected[idx]["id"] = delta_tc.id
    if delta_tc.function:
        if delta_tc.function.name:
            collected[idx]["function"]["name"] += delta_tc.function.name
        if delta_tc.function.arguments:
            collected[idx]["function"]["arguments"] += delta_tc.function.arguments
