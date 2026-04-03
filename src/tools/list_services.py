import requests
from config import TEMPO_URL, MIMIR_URL

TOOL_NAME = "list_services"

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
    services = {}

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
