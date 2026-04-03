INPUT_COST_PER_M = 2.50
CACHED_COST_PER_M = 1.25
OUTPUT_COST_PER_M = 15.00


class CostTracker:
    def __init__(self):
        self._input_tokens = 0
        self._cached_tokens = 0
        self._output_tokens = 0

    def record(self, usage):
        self._input_tokens += usage.prompt_tokens
        self._output_tokens += usage.completion_tokens
        cached = getattr(usage, "prompt_tokens_details", None)
        if cached and hasattr(cached, "cached_tokens"):
            self._cached_tokens += cached.cached_tokens

    def total_tokens(self) -> int:
        return self._input_tokens + self._output_tokens

    def estimated_cost(self) -> float:
        uncached_input = self._input_tokens - self._cached_tokens
        return (
            (uncached_input / 1_000_000) * INPUT_COST_PER_M
            + (self._cached_tokens / 1_000_000) * CACHED_COST_PER_M
            + (self._output_tokens / 1_000_000) * OUTPUT_COST_PER_M
        )

    def to_dict(self) -> dict:
        return {
            "input_tokens": self._input_tokens,
            "cached_tokens": self._cached_tokens,
            "output_tokens": self._output_tokens,
            "estimated_cost": self.estimated_cost(),
        }
