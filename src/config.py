"""Central configuration for the Investigate CLI.

All settings are loaded once at import time. Environment variables (from .env
or the shell) override the defaults below. Every other module imports constants
from here — this is the single source of truth for URLs, model names, and limits.
"""

import os
from pathlib import Path


# ---------------------------------------------------------------------------
# .env loader (replaces python-dotenv to avoid an extra dependency)
# ---------------------------------------------------------------------------

def _load_env():
    """Load key=value pairs from the project-root .env file.

    Uses os.environ.setdefault so real environment variables always take
    precedence over .env values (important for Docker/CI overrides).
    """
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_env()


# ---------------------------------------------------------------------------
# LLM model selection
# ---------------------------------------------------------------------------

# Primary model — used for reasoning, correlation, and diagnosis
MODEL = os.getenv("MODEL", "gpt-5.4")

# Cheaper model — used only for the first "sweep" call in V4 (model routing)
# The sweep call does broad parallel queries, not deep reasoning, so a cheaper
# model saves ~50% on that call without hurting quality.
SWEEP_MODEL = os.getenv("SWEEP_MODEL", "gpt-4.1")

# Judge model — used by the eval framework to score agent diagnoses
JUDGE_MODEL = os.getenv("JUDGE_MODEL", "gpt-5.4")


# ---------------------------------------------------------------------------
# Observability backend URLs (the LGTM stack)
# ---------------------------------------------------------------------------

LOKI_URL = os.getenv("LOKI_URL", "http://localhost:3100")      # Logs
MIMIR_URL = os.getenv("MIMIR_URL", "http://localhost:9090")     # Metrics (Prometheus-compatible)
TEMPO_URL = os.getenv("TEMPO_URL", "http://localhost:3200")     # Traces
GRAFANA_URL = os.getenv("GRAFANA_URL", "http://localhost:3001") # Dashboard UI


# ---------------------------------------------------------------------------
# Web server settings (FastAPI backend for the Grafana plugin)
# ---------------------------------------------------------------------------

WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("WEB_PORT", "8000"))


# ---------------------------------------------------------------------------
# Agent loop limits
# ---------------------------------------------------------------------------

# Maximum LLM calls per investigation (safety guardrail against infinite loops)
MAX_AGENT_ITERATIONS = 20

# Max tool calls the agent can make in a single LLM response.
# OpenAI may return more tool_calls than this; extras are skipped with a message.
MAX_TOOL_CALLS_PER_TURN = 3

# Tool output is truncated to this many characters before being added to context.
# Prevents a single large query result from consuming the entire context window.
TOOL_OUTPUT_MAX_CHARS = 15_000

# Max retries for transient API errors (rate limits, timeouts)
MAX_RETRIES = 3

# Context window size (used for percentage calculations in status display)
MAX_CONTEXT_TOKENS = 1_000_000
