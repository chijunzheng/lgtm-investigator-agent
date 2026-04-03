import time
import json
from config import MICRO_COMPACT_KEEP_TURNS, TOOL_OUTPUT_MAX_CHARS

BYTES_PER_TOKEN = 4


class ContextManager:
    def __init__(self):
        self._messages = []
        self._query_cache = {}
        self.cache_hit_count = 0
        self.micro_compact_count = 0

    def add_user_message(self, content: str):
        self._messages.append({"role": "user", "content": content})

    def add_assistant_message(self, message: dict):
        msg = {**message, "_timestamp": time.time()}
        self._messages.append(msg)

    def add_tool_result(self, tool_call_id: str, result: str, tool_name: str):
        if len(result) > TOOL_OUTPUT_MAX_CHARS:
            result = result[:TOOL_OUTPUT_MAX_CHARS] + f"\n\n[truncated -- {len(result)} chars total]"
        self._messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": result,
            "_tool_name": tool_name,
            "_timestamp": time.time(),
        })

    def add_system_context(self, content: str):
        """Inject context (e.g., topology) as first user message."""
        self._messages.insert(0, {"role": "user", "content": content})

    def get_messages(self) -> list[dict]:
        return [
            {k: v for k, v in m.items() if not k.startswith("_")}
            for m in self._messages
        ]

    def prepare(self):
        """Run micro-compact before each LLM call."""
        self._micro_compact()

    def _micro_compact(self):
        """Clear tool results older than N assistant turns."""
        turns_seen = 0
        cutoff_idx = len(self._messages)
        for i in range(len(self._messages) - 1, -1, -1):
            if self._messages[i].get("role") == "assistant":
                turns_seen += 1
            if turns_seen >= MICRO_COMPACT_KEEP_TURNS:
                cutoff_idx = i
                break

        compacted = 0
        for i in range(cutoff_idx):
            msg = self._messages[i]
            if msg.get("role") == "tool" and not msg["content"].startswith("[Compacted"):
                original = msg["content"]
                lines = original.strip().split("\n")
                first_line = lines[0][:200]
                last_line = lines[-1][:200] if len(lines) > 1 else ""
                summary = f"{first_line}\n...\n{last_line}" if last_line else first_line
                msg["content"] = f"[Compacted -- stale tool result. Summary: {summary}]"
                compacted += 1

        if compacted > 0:
            self.micro_compact_count += compacted
            print(f"  [context] micro-compacted {compacted} stale tool result(s)")

    def get_or_execute(self, tool_name: str, args_str: str, execute_fn) -> tuple[str, bool]:
        """Query dedup cache. Returns (result, was_cached)."""
        cache_key = f"{tool_name}:{args_str}"
        if cache_key in self._query_cache:
            self.cache_hit_count += 1
            return self._query_cache[cache_key], True
        result = execute_fn()
        self._query_cache[cache_key] = result
        return result, False

    def estimate_tokens(self) -> int:
        return len(json.dumps(self.get_messages())) // BYTES_PER_TOKEN
