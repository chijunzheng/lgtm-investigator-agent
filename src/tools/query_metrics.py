import requests
from config import MIMIR_URL, TOOL_OUTPUT_MAX_CHARS

TOOL_NAME = "query_metrics"

DEFINITION = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": "Query metrics from Prometheus/Mimir using PromQL. Supports both instant and range queries.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "PromQL query string. Examples: 'rate(http_server_duration_seconds_count{service_name=\"payment\"}[5m])', 'up'",
                },
                "start": {
                    "type": "string",
                    "description": "Start time in ISO8601 format. Omit for instant query.",
                },
                "end": {
                    "type": "string",
                    "description": "End time in ISO8601 format. Omit for instant query.",
                },
                "step": {
                    "type": "string",
                    "description": "Step interval for range queries (default '60s'). Examples: '15s', '60s', '5m'",
                },
            },
            "required": ["query"],
        },
    },
}


def execute(*, query: str, start: str = None, end: str = None, step: str = "60s",
            metadata_headers: bool = False, error_enrichment: bool = False,
            **_kwargs) -> str:
    is_range = start is not None and end is not None

    try:
        if is_range:
            r = requests.get(
                f"{MIMIR_URL}/api/v1/query_range",
                params={"query": query, "start": start, "end": end, "step": step},
                timeout=30,
            )
        else:
            params = {"query": query}
            if end:
                params["time"] = end
            r = requests.get(f"{MIMIR_URL}/api/v1/query", params=params, timeout=30)
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
    result_type = data.get("data", {}).get("resultType", "")
    results = data.get("data", {}).get("result", [])

    lines = []
    series_count = 0
    for series in results[:20]:
        series_count += 1
        metric = series.get("metric", {})
        label_str = ", ".join(f'{k}="{v}"' for k, v in metric.items())

        if result_type == "matrix":
            values = series.get("values", [])
            last_values = values[-5:] if len(values) > 5 else values
            points = " | ".join(f"{ts}: {val}" for ts, val in last_values)
            total_note = f" ({len(values)} total)" if len(values) > 5 else ""
            lines.append(f"{{{label_str}}} {points}{total_note}")
        elif result_type == "vector":
            ts, val = series.get("value", [0, ""])
            lines.append(f"{{{label_str}}} {val}")
        else:
            lines.append(f"{{{label_str}}} {series}")

    truncated_note = f" (showing 20 of {len(results)})" if len(results) > 20 else ""

    if metadata_headers:
        header = f"[{TOOL_NAME}] query={query}\n"
        if not lines:
            return header + "[0 results -- no matching data in this window]"
        header += f"[{len(results)} series{truncated_note} | type: {result_type}]\n\n"
        output = header + "\n".join(lines)
    else:
        if not lines:
            return "No metric data found."
        output = "\n".join(lines)

    if len(output) > TOOL_OUTPUT_MAX_CHARS:
        output = output[:TOOL_OUTPUT_MAX_CHARS] + f"\n\n[truncated -- {len(output)} chars total]"

    return output


def _enrich_error(query: str, error: str) -> str:
    hints = []
    error_lower = error.lower()
    if "unknown metric" in error_lower or "not found" in error_lower or "no data" in error_lower:
        try:
            r = requests.get(f"{MIMIR_URL}/api/v1/label/__name__/values", timeout=10)
            all_names = r.json().get("data", [])
            keywords = [w for w in query.split("(")[0].replace("_", " ").split() if len(w) > 3]
            similar = [n for n in all_names if any(k in n for k in keywords)][:5]
            if similar:
                hints.append(f"[available metrics] {', '.join(similar)}")
        except Exception:
            pass
        hints.append("[hint] This system uses OpenTelemetry naming: http_server_duration_seconds_*, rpc_server_duration_milliseconds_*")
    if "label" in error_lower and ("not found" in error_lower or "unknown" in error_lower):
        try:
            r = requests.get(f"{MIMIR_URL}/api/v1/labels", timeout=10)
            labels = r.json().get("data", [])
            hints.append(f"[available labels] {', '.join(labels[:15])}")
        except Exception:
            pass
    if "parse error" in error_lower:
        hints.append("[hint] Check PromQL syntax. Example: rate(http_server_duration_seconds_count{service_name=\"payment\"}[5m])")
    return "\n".join(hints) if hints else "[hint] Check query syntax and parameter names"
