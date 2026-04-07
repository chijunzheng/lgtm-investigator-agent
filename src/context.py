"""Manages the conversation history (messages) sent to the LLM on each call.

This module solves two problems that arise in multi-turn agent investigations:

1. **Context bloat**: Each tool call adds thousands of characters of raw data
   (log lines, metric values, trace spans). After 10+ tool calls, the context
   becomes too large and the model loses focus. Micro-compact fixes this by
   replacing old tool results with short markers.

2. **Redundant queries**: The LLM sometimes re-queries the same data it already
   has. The query dedup cache catches these and returns the cached result.

Architecture:
  - Messages are stored as a list of OpenAI-format dicts (role, content, etc.)
  - Internal metadata (prefixed with "_") is stripped before sending to the API
  - Micro-compact runs before every LLM call (in V3/V4 only)
"""

import time
import json
from config import TOOL_OUTPUT_MAX_CHARS

# Rough estimate: 1 token ≈ 4 bytes of JSON text.
# Not exact, but good enough for progress display and percentage calculations.
BYTES_PER_TOKEN = 4

# How many recent tool results to keep in full. Older results get cleared.
# 5 is a sweet spot: enough for the current investigation step, but old
# sweep results from 3 turns ago don't clutter the context.
KEEP_RECENT_TOOL_RESULTS = 5

# Prefix used to mark cleared tool results (so we don't clear them again)
CLEARED_PREFIX = "[Cleared:"


class ContextManager:
    """Maintains the message history and provides context optimization.

    The agent loop interacts with this class in a cycle:
      1. add_user_message()       — user's incident description
      2. prepare()                — micro-compact old tool results (V3/V4)
      3. get_messages()           — clean messages sent to OpenAI API
      4. add_assistant_message()  — LLM's response (reasoning + tool calls)
      5. add_tool_result()        — results from executed tools
      6. → back to step 2 (loop until LLM stops calling tools)
    """

    def __init__(self):
        self._messages = []         # Full message history with internal metadata
        self._query_cache = {}      # Dedup cache: "tool_name:args" → result string
        self.cache_hit_count = 0    # Counter for eval metrics
        self.micro_compact_count = 0  # Counter for eval metrics

    # -- Message management --------------------------------------------------

    def add_user_message(self, content: str):
        """Add the user's input (incident description or follow-up question)."""
        self._messages.append({"role": "user", "content": content})

    def add_assistant_message(self, message: dict):
        """Store the LLM's response (may contain text, tool_calls, or both).

        We add an internal _timestamp so micro-compact can reason about
        message ordering if needed.
        """
        msg = {**message, "_timestamp": time.time()}
        self._messages.append(msg)

    def add_tool_result(self, tool_call_id: str, result: str, tool_name: str, args_preview: str = ""):
        """Store a tool execution result.

        Truncates oversized results immediately (before micro-compact) to
        prevent any single tool call from blowing up the context.

        Internal fields (_tool_name, _args_preview) are used by micro-compact
        to build the cleared marker message, then stripped before API calls.
        """
        if len(result) > TOOL_OUTPUT_MAX_CHARS:
            result = result[:TOOL_OUTPUT_MAX_CHARS] + f"\n\n[truncated -- {len(result)} chars total]"
        self._messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": result,
            "_tool_name": tool_name,
            "_args_preview": args_preview,
            "_timestamp": time.time(),
        })

    def add_system_context(self, content: str):
        """Inject context (e.g., service topology) as the first user message.

        Inserted at position 0 so the LLM sees the system topology before
        the user's actual question. This gives the model awareness of which
        services exist before it starts investigating.
        """
        self._messages.insert(0, {"role": "user", "content": content})

    def get_messages(self) -> list[dict]:
        """Return messages in OpenAI API format (strips internal _ fields).

        The API only accepts role, content, tool_call_id, and tool_calls.
        Our internal fields (_timestamp, _tool_name, _args_preview) would
        cause API errors if included.
        """
        return [
            {k: v for k, v in m.items() if not k.startswith("_")}
            for m in self._messages
        ]

    # -- Context optimization ------------------------------------------------

    def prepare(self):
        """Run micro-compact before each LLM call (V3/V4 only).

        Called at the top of the agent loop, before the LLM sees the messages.
        This ensures the context is compact before every reasoning step.
        """
        self._micro_compact()

    def _micro_compact(self):
        """Replace old tool results with short markers to save context space.

        How it works:
          1. Find all tool results that haven't already been cleared
          2. Keep the N most recent ones (KEEP_RECENT_TOOL_RESULTS = 5)
          3. Replace older ones with: "[Cleared: tool_name(args) — see your summary above]"

        Why this works:
          The LLM writes a text summary of findings after each tool call (in its
          assistant message). That summary persists even after the raw tool output
          is cleared. So the model can still reference its own notes about what
          the data showed — it just can't see the raw log lines anymore.

        This is count-based (not token-based) for simplicity. Claude Code's own
        micro-compact uses a similar approach.
        """
        # Find all tool result positions that haven't been cleared yet
        tool_positions = [
            i for i, m in enumerate(self._messages)
            if m.get("role") == "tool" and not m["content"].startswith(CLEARED_PREFIX)
        ]

        # Keep the most recent N, clear the rest
        to_clear = tool_positions[:-KEEP_RECENT_TOOL_RESULTS] if len(tool_positions) > KEEP_RECENT_TOOL_RESULTS else []

        compacted = 0
        compacted_tokens = 0
        for i in to_clear:
            msg = self._messages[i]
            original_len = len(msg["content"])
            tool_name = msg.get("_tool_name", "tool")
            args = msg.get("_args_preview", "")
            cleared = f"{CLEARED_PREFIX} {tool_name}({args}) — see your summary above]"
            # Only replace if the cleared message is actually shorter
            if original_len > len(cleared):
                compacted_tokens += (original_len - len(cleared)) // BYTES_PER_TOKEN
                msg["content"] = cleared
                compacted += 1

        if compacted > 0:
            self.micro_compact_count += compacted
            print(f"  [context] cleared {compacted} old tool result(s), saved ~{compacted_tokens:,} tokens")

    # -- Query dedup cache ---------------------------------------------------

    def get_or_execute(self, tool_name: str, args_str: str, execute_fn) -> tuple[str, bool]:
        """Check if we've already run this exact query; if so, return cached result.

        The LLM sometimes re-queries the same data (e.g., asking for the same
        service's logs twice). This prevents redundant HTTP calls to the backends.

        Returns:
            (result_string, was_cached) — was_cached=True means we skipped the HTTP call.
        """
        cache_key = f"{tool_name}:{args_str}"
        if cache_key in self._query_cache:
            self.cache_hit_count += 1
            return self._query_cache[cache_key], True
        result = execute_fn()
        self._query_cache[cache_key] = result
        return result, False

    # -- Utility -------------------------------------------------------------

    def estimate_tokens(self) -> int:
        """Rough token count of the current context (for status display)."""
        return len(json.dumps(self.get_messages())) // BYTES_PER_TOKEN
