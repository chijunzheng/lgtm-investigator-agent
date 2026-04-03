import requests
from config import LOKI_URL, TOOL_OUTPUT_MAX_CHARS

TOOL_NAME = "query_logs"

DEFINITION = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": "Query logs from Loki using LogQL. Returns matching log lines within the time window.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "LogQL query string. Examples: '{service_name=\"payment\"}', '{service_name=~\"payment|checkout\"} |= \"error\"'",
                },
                "start": {
                    "type": "string",
                    "description": "Start time in ISO8601 format (e.g., '2026-04-02T22:15:00Z')",
                },
                "end": {
                    "type": "string",
                    "description": "End time in ISO8601 format (e.g., '2026-04-02T22:20:00Z')",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of log lines to return (default 100)",
                },
            },
            "required": ["query", "start", "end"],
        },
    },
}


def execute(*, query: str, start: str, end: str, limit: int = 100,
            metadata_headers: bool = False, error_enrichment: bool = False,
            **_kwargs) -> str:
    try:
        r = requests.get(
            f"{LOKI_URL}/loki/api/v1/query_range",
            params={
                "query": query,
                "start": start,
                "end": end,
                "limit": limit,
                "direction": "backward",
            },
            timeout=30,
        )
        r.raise_for_status()
    except requests.HTTPError as e:
        error_body = e.response.text if e.response else str(e)
        if error_enrichment:
            enrichment = _enrich_error(query, error_body)
            return f"[{TOOL_NAME}] ERROR: {error_body}\n{enrichment}"
        return f"[{TOOL_NAME}] ERROR: {error_body}"
    except requests.RequestException as e:
        return f"[{TOOL_NAME}] ERROR: connection failed: {e}"

    data = r.json()
    results = data.get("data", {}).get("result", [])

    lines = []
    for stream in results:
        labels = stream.get("stream", {})
        service = labels.get("service_name", labels.get("job", "unknown"))
        for ts, line in stream.get("values", []):
            lines.append(f"[{service}] {line}")

    if metadata_headers:
        header = f"[{TOOL_NAME}] query={query}\n"
        if not lines:
            return header + "[0 results -- no matching data in this window]"
        header += f"[{len(lines)} results | showing newest {min(len(lines), limit)} | window: {start} to {end}]\n\n"
        output = header + "\n".join(lines)
    else:
        if not lines:
            return "No log lines found."
        output = "\n".join(lines)

    if len(output) > TOOL_OUTPUT_MAX_CHARS:
        output = output[:TOOL_OUTPUT_MAX_CHARS] + f"\n\n[truncated -- {len(output)} chars total]"

    return output


def _enrich_error(query: str, error: str) -> str:
    hints = []
    if "parse error" in error.lower() or "syntax error" in error.lower():
        hints.append("[hint] Check LogQL syntax. Example: {service_name=\"payment\"} |= \"error\"")
        try:
            r = requests.get(f"{LOKI_URL}/loki/api/v1/labels", timeout=10)
            labels = r.json().get("data", [])
            hints.append(f"[available labels] {', '.join(labels[:15])}")
        except Exception:
            pass
    if "not found" in error.lower() or "unknown" in error.lower():
        try:
            r = requests.get(f"{LOKI_URL}/loki/api/v1/label/service_name/values", timeout=10)
            services = r.json().get("data", [])
            if services:
                hints.append(f"[available service_name values] {', '.join(services[:20])}")
        except Exception:
            pass
    return "\n".join(hints) if hints else "[hint] Check query syntax and label names"
