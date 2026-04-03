import os
from pathlib import Path


def _load_env():
    """Load .env from project root (no external dependency)."""
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

MODEL = os.getenv("MODEL", "gpt-5.4")
JUDGE_MODEL = os.getenv("JUDGE_MODEL", "gpt-5.4")

LOKI_URL = os.getenv("LOKI_URL", "http://localhost:3100")
MIMIR_URL = os.getenv("MIMIR_URL", "http://localhost:9090")
TEMPO_URL = os.getenv("TEMPO_URL", "http://localhost:3200")

MAX_CONTEXT_TOKENS = 1_000_000
MICRO_COMPACT_KEEP_TURNS = 3

MAX_AGENT_ITERATIONS = 20
MAX_TOOL_CALLS_PER_TURN = 10
TOOL_OUTPUT_MAX_CHARS = 15_000
MAX_RETRIES = 3
