import time
from concurrent.futures import ThreadPoolExecutor
import openai
from openai import NOT_GIVEN
from rich.console import Console
from rich.markdown import Markdown
from context import ContextManager
from tools_registry import TOOL_DEFINITIONS, dispatch
from cost_tracker import CostTracker
from config import MODEL, MAX_AGENT_ITERATIONS, MAX_TOOL_CALLS_PER_TURN, MAX_RETRIES

_console = Console()

# Diagnosis fields to render as section headers
_DIAGNOSIS_FIELDS = [
    "Root Cause:", "Confidence:", "Evidence:", "Contradictions:",
    "Contradicting Evidence:", "Not Investigated:", "Remediation:",
]


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
    def __init__(self, client: openai.OpenAI, system_prompt: str,
                 tools_enabled: bool = True,
                 context_management_enabled: bool = False,
                 tool_metadata_headers: bool = False,
                 error_enrichment: bool = False,
                 parallel_tool_calls: bool = False,
                 inject_topology: bool = False):
        self.client = client
        self.system_prompt = system_prompt
        self.tools_enabled = tools_enabled
        self.context_management_enabled = context_management_enabled
        self.tool_metadata_headers = tool_metadata_headers
        self.error_enrichment = error_enrichment
        self.parallel_tool_calls = parallel_tool_calls
        self.inject_topology = inject_topology
        self._topology_injected = False
        self.ctx = ContextManager()
        self.cost = CostTracker()
        self.total_tool_calls = 0
        self.total_llm_calls = 0
        self.trace = []

    def run(self, user_input: str, time_window: dict = None) -> str:
        """Process user message through the agent loop. Returns final text."""

        if time_window:
            user_input += (
                f"\n\n[Investigation window: {time_window['start']} "
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

        self.ctx.add_user_message(user_input)
        full_response = ""

        while self.total_llm_calls < MAX_AGENT_ITERATIONS:
            # -- 1. Context management (V3 only) --
            if self.context_management_enabled:
                self.ctx.prepare()

            # -- 2. Call OpenAI API with retry --
            self.total_llm_calls += 1
            api_kwargs = {
                "model": MODEL,
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
                self.cost.record(usage)

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

            # -- 6. If no tool calls -> done --
            tool_calls = message.get("tool_calls", [])
            if not tool_calls:
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

            if self.parallel_tool_calls and len(executable) > 1:
                self._execute_tools_parallel(executable)
            else:
                self._execute_tools_sequential(executable)

            if self.context_management_enabled:
                self._print_status()

            print()  # space after tool calls, before next thinking

        return full_response

    def _execute_one_tool(self, call: dict) -> tuple[str, str, str, bool]:
        """Execute a single tool call. Returns (call_id, name, result, was_cached)."""
        name = call["function"]["name"]
        args_str = call["function"]["arguments"]
        was_cached = False

        if self.context_management_enabled:
            result, was_cached = self.ctx.get_or_execute(
                name, args_str,
                lambda n=name, a=args_str: dispatch(n, a, self.tool_metadata_headers, self.error_enrichment),
            )
            if was_cached:
                _console.print(f"  [dim][cache] {name} hit -- reusing cached result[/dim]")
        else:
            result = dispatch(name, args_str, self.tool_metadata_headers, self.error_enrichment)

        return call["id"], name, args_str, result, was_cached

    def _record_tool_result(self, call_id: str, name: str, args_str: str, result: str, was_cached: bool):
        """Store tool result in context and trace."""
        self.ctx.add_tool_result(call_id, result, name)
        self.total_tool_calls += 1

        is_error = result.startswith(f"[{name}] ERROR") or result.startswith("Tool error")
        self.trace.append({
            "type": "tool_call",
            "tool": name,
            "args": args_str[:300],
            "result_preview": result[:300],
            "result_length": len(result),
            "cached": was_cached,
            "error": is_error,
            "timestamp": time.time(),
        })

    def _execute_tools_sequential(self, calls: list):
        """Execute tool calls one at a time."""
        for call in calls:
            call_id, name, args_str, result, was_cached = self._execute_one_tool(call)
            self._record_tool_result(call_id, name, args_str, result, was_cached)

    def _execute_tools_parallel(self, calls: list):
        """Execute tool calls concurrently via ThreadPoolExecutor."""
        with ThreadPoolExecutor(max_workers=len(calls)) as pool:
            futures = [pool.submit(self._execute_one_tool, call) for call in calls]
            results = [f.result() for f in futures]
        # Record in original order (preserves message ordering for API)
        for call_id, name, args_str, result, was_cached in results:
            self._record_tool_result(call_id, name, args_str, result, was_cached)

    def _call_with_retry(self, **kwargs):
        """Call OpenAI with streaming + retry. Returns (message_dict, usage) or None."""
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

    def get_stats(self) -> dict:
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
        self.ctx = ContextManager()
        self.cost = CostTracker()
        self.total_tool_calls = 0
        self.total_llm_calls = 0
        self.trace = []
        self._topology_injected = False


def _accumulate_tool_call(collected: list, delta_tc):
    """Accumulate streamed tool call deltas into complete tool calls."""
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
