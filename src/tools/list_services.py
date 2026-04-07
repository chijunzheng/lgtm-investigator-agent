"""List all services in the system with their available signal types.

This tool takes no parameters — it aggregates service names from Tempo (traces)
and Mimir (metrics) to build a system topology map. The agent uses this to
understand what services exist before starting an investigation.

In V2+, this is called automatically before the first LLM turn (inject_topology)
so the model already knows the system layout. It can also be called explicitly
by the LLM via the tool interface.
"""

import requests
from config import LOKI_URL, TEMPO_URL, MIMIR_URL

TOOL_NAME = "list_services"

# OpenAI function schema. Note: no parameters — this is a "discovery" tool.
DEFINITION = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": (
            "List all services in the system with their signal types "
            "(logs, metrics, traces). Use this to understand the system topology."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
}


def execute(*, metadata_headers: bool = False, error_enrichment: bool = False,
            **_kwargs) -> str:
    """Aggregate service names from Tempo, Mimir, and Loki, showing which signals are available.

    Queries 3 backends independently and merges results. If one backend is
    down, the other's results are still returned (graceful degradation).

    Output example:
      payment: metrics, traces
      frontend: metrics, traces
      kafka: metrics
    """
    services = {}

    try:
        r = requests.get(
            f"{LOKI_URL}/loki/api/v1/label/service_name/values",
            timeout=15,
        )
        r.raise_for_status()
        for name in r.json().get("data", []):
            services.setdefault(name, set()).add("logs")
    except Exception as e:
        services["_error_loki"] = {f"loki error: {e}"}

    try:
        r = requests.get(
            f"{TEMPO_URL}/api/search/tag/service.name/values",
            timeout=15,
        )
        r.raise_for_status()
        for val in r.json().get("tagValues", []):
            name = val.get("value", val) if isinstance(val, dict) else str(val)
            services.setdefault(name, set()).add("traces")
    except Exception as e:
        services["_error_tempo"] = {f"tempo error: {e}"}

    try:
        r = requests.get(
            f"{MIMIR_URL}/api/v1/label/service_name/values",
            timeout=15,
        )
        r.raise_for_status()
        for name in r.json().get("data", []):
            services.setdefault(name, set()).add("metrics")
    except Exception as e:
        services["_error_mimir"] = {f"mimir error: {e}"}

    error_entries = {k: v for k, v in services.items() if k.startswith("_error")}
    real_services = {k: v for k, v in services.items() if not k.startswith("_error")}

    lines = []
    for name in sorted(real_services.keys()):
        signals = sorted(real_services[name])
        lines.append(f"  {name}: {', '.join(signals)}")

    if metadata_headers:
        header = f"[{TOOL_NAME}]\n"
        header += f"[{len(real_services)} services found]\n\n"
        output = header + "\n".join(lines)
    else:
        if not lines:
            return "No services found."
        output = f"{len(real_services)} services:\n" + "\n".join(lines)

    for k, v in error_entries.items():
        output += f"\n\nNote: {list(v)[0]}"

    return output
