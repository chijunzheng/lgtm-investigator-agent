"""Track cumulative LLM token usage and estimated cost across an investigation.

OpenAI charges per 1M tokens with different rates for:
  - Input tokens (the prompt/context sent to the model)
  - Cached input tokens (repeated prefixes like system prompt — 50% cheaper)
  - Output tokens (the model's response — most expensive)

The agent loop calls cost.record() after every LLM call, and the eval
framework reads cost.to_dict() to compare versions on cost efficiency.
"""

# Pricing per 1M tokens (USD) — update when OpenAI changes pricing
MODEL_PRICING = {
    "gpt-5.4": {"input": 2.50, "cached": 1.25, "output": 15.00},
    "gpt-4.1": {"input": 1.00, "cached": 0.50, "output": 4.00},
}

DEFAULT_PRICING = MODEL_PRICING["gpt-5.4"]


class CostTracker:
    """Accumulates token counts and dollar cost across multiple LLM calls."""

    def __init__(self):
        self._input_tokens = 0
        self._cached_tokens = 0
        self._output_tokens = 0
        self._cost = 0.0

    def record(self, usage, model: str = None):
        """Record token usage from a single OpenAI API response.

        Args:
            usage: The usage object from OpenAI's response (has prompt_tokens,
                   completion_tokens, and optionally prompt_tokens_details).
            model: Which model was used (for looking up the right pricing).
        """
        self._input_tokens += usage.prompt_tokens
        self._output_tokens += usage.completion_tokens

        # OpenAI's prompt caching: when the system prompt + early messages
        # are identical across calls, the API caches them and charges less.
        # The cached token count is nested inside prompt_tokens_details.
        cached_tokens = 0
        cached = getattr(usage, "prompt_tokens_details", None)
        if cached and hasattr(cached, "cached_tokens"):
            cached_tokens = cached.cached_tokens
            self._cached_tokens += cached_tokens

        # Calculate cost: uncached input + cached input + output
        pricing = MODEL_PRICING.get(model, DEFAULT_PRICING)
        uncached_input = usage.prompt_tokens - cached_tokens
        self._cost += (
            (uncached_input / 1_000_000) * pricing["input"]
            + (cached_tokens / 1_000_000) * pricing["cached"]
            + (usage.completion_tokens / 1_000_000) * pricing["output"]
        )

    def total_tokens(self) -> int:
        """Total tokens consumed (input + output) across all calls."""
        return self._input_tokens + self._output_tokens

    def estimated_cost(self) -> float:
        """Cumulative estimated cost in USD."""
        return self._cost

    def to_dict(self) -> dict:
        """Snapshot of all tracking data (used by eval framework and stats display)."""
        return {
            "input_tokens": self._input_tokens,
            "cached_tokens": self._cached_tokens,
            "output_tokens": self._output_tokens,
            "estimated_cost": self.estimated_cost(),
        }
