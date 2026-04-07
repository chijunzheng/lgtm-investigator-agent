"""Query distributed traces from Tempo.

Traces show the full journey of a request across microservices — which services
were called, how long each took, and which ones errored. The LLM uses this to
follow the dependency chain during investigations.

Two modes:
  - Search: find traces matching criteria (TraceQL or service name filter)
  - Detail: drill into a specific trace's span tree (by trace_id)

Typical workflow: LLM searches first → finds an interesting traceID → gets detail.
"""

from datetime import datetime, timezone
import requests
from config import TEMPO_URL, TOOL_OUTPUT_MAX_CHARS


def _to_epoch(ts: str) -> int:
    """Convert ISO8601 timestamp to Unix epoch seconds.

    Tempo's search API requires epoch seconds, but the LLM generates
    ISO8601 timestamps (e.g., "2026-04-02T22:15:00Z"). This bridges the gap.
    """
    dt = datetime.fromisoformat(ts)
    return int(dt.timestamp())

TOOL_NAME = "query_traces"

# OpenAI function schema. Note: no required params — the LLM can search by
# TraceQL (q), by service_name, or get detail by trace_id.
DEFINITION = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": (
            "Search traces in Tempo or get trace detail. "
            "Use 'q' for TraceQL search, or 'service_name' for simple search. "
            "Use 'trace_id' to get full span detail for a specific trace."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "q": {
                    "type": "string",
                    "description": "TraceQL query. Example: '{resource.service.name=\"payment\" && status=error}'",
                },
                "service_name": {
                    "type": "string",
                    "description": "Filter traces by service name (simple search, alternative to TraceQL)",
                },
                "start": {
                    "type": "string",
                    "description": "Start time in ISO8601 format",
                },
                "end": {
                    "type": "string",
                    "description": "End time in ISO8601 format",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of traces to return (default 20)",
                },
                "trace_id": {
                    "type": "string",
                    "description": "Specific trace ID to retrieve full span detail",
                },
            },
        },
    },
}


def execute(*, q: str = None, service_name: str = None,
            start: str = None, end: str = None, limit: int = 20,
            trace_id: str = None,
            metadata_headers: bool = False, error_enrichment: bool = False,
            **_kwargs) -> str:
    """Route to search or detail mode based on which parameters the LLM provided."""
    # If trace_id is given, drill into that specific trace's spans
    if trace_id:
        return _get_trace_detail(trace_id, metadata_headers)

    return _search_traces(
        q=q, service_name=service_name,
        start=start, end=end, limit=limit,
        metadata_headers=metadata_headers,
        error_enrichment=error_enrichment,
    )


def _search_traces(*, q: str = None, service_name: str = None,
                   start: str = None, end: str = None, limit: int = 20,
                   metadata_headers: bool = False,
                   error_enrichment: bool = False) -> str:
    """Search for traces via Tempo's /api/search endpoint.

    Two search modes:
      - TraceQL (q param): powerful query language, e.g. '{status=error}'
      - Simple filter (service_name param): converted to tags=service.name=X

    Output: trace-level summaries (root service, operation, duration, span count).
    """
    params = {"limit": limit}
    if q:
        params["q"] = q
    if service_name:
        params["tags"] = f"service.name={service_name}"
    if start:
        params["start"] = _to_epoch(start)
    if end:
        params["end"] = _to_epoch(end)

    try:
        r = requests.get(f"{TEMPO_URL}/api/search", params=params, timeout=30)
        r.raise_for_status()
    except requests.HTTPError as e:
        error_body = e.response.text if e.response else str(e)
        if error_enrichment:
            enrichment = _enrich_error(q or service_name or "", error_body)
            return f"[{TOOL_NAME}] ERROR: {error_body}\n{enrichment}"
        return f"[{TOOL_NAME}] ERROR: {error_body}"
    except requests.RequestException as e:
        return f"[{TOOL_NAME}] ERROR: connection failed: {e}"

    data = r.json()
    traces = data.get("traces", [])

    lines = []
    for trace in traces:
        tid = trace.get("traceID", "?")
        root_service = trace.get("rootServiceName", "?")
        root_name = trace.get("rootTraceName", "?")
        duration_ms = trace.get("durationMs", 0)
        span_count = trace.get("spanCount", 0) if "spanCount" in trace else "?"
        start_time = trace.get("startTimeUnixNano", "")

        lines.append(
            f"traceID={tid} service={root_service} "
            f"name={root_name} duration={duration_ms}ms spans={span_count}"
        )

    query_desc = q or f"service.name={service_name}" if service_name else "all"

    if metadata_headers:
        header = f"[{TOOL_NAME}] search={query_desc}\n"
        if not lines:
            return header + "[0 results -- no matching traces in this window]"
        header += f"[{len(traces)} traces | showing {min(len(traces), limit)}]\n\n"
        output = header + "\n".join(lines)
    else:
        if not lines:
            return "No traces found."
        output = "\n".join(lines)

    if len(output) > TOOL_OUTPUT_MAX_CHARS:
        output = output[:TOOL_OUTPUT_MAX_CHARS] + f"\n\n[truncated -- {len(output)} chars total]"

    return output


def _get_trace_detail(trace_id: str, metadata_headers: bool = False) -> str:
    """Fetch all spans for a single trace from /api/traces/{id}.

    Parses the OTLP JSON format (batches → scopeSpans → spans) to extract:
      service name, operation name, duration, error status, parent span

    Results are sorted by duration descending — the slowest spans (likely
    the bottleneck) appear first, helping the LLM find the root cause faster.
    """
    try:
        r = requests.get(
            f"{TEMPO_URL}/api/traces/{trace_id}",
            headers={"Accept": "application/json"},
            timeout=30,
        )
        r.raise_for_status()
    except requests.HTTPError as e:
        error_body = e.response.text if e.response else str(e)
        return f"[{TOOL_NAME}] ERROR: {error_body}"
    except requests.RequestException as e:
        return f"[{TOOL_NAME}] ERROR: connection failed: {e}"

    data = r.json()
    spans = []

    for batch in data.get("batches", []):
        resource_attrs = {}
        for attr in batch.get("resource", {}).get("attributes", []):
            resource_attrs[attr.get("key", "")] = _attr_value(attr.get("value", {}))

        service = resource_attrs.get("service.name", "unknown")

        for scope_spans in batch.get("scopeSpans", []):
            for span in scope_spans.get("spans", []):
                start_ns = int(span.get("startTimeUnixNano", "0"))
                end_ns = int(span.get("endTimeUnixNano", "0"))
                duration_ms = (end_ns - start_ns) / 1_000_000

                status_code = span.get("status", {}).get("code", 0)
                status = "ERROR" if status_code == 2 else "OK"

                spans.append({
                    "service": service,
                    "name": span.get("name", "?"),
                    "duration_ms": round(duration_ms, 1),
                    "status": status,
                    "span_id": span.get("spanId", "?"),
                    "parent_span_id": span.get("parentSpanId", ""),
                })

    spans.sort(key=lambda s: s["duration_ms"], reverse=True)

    lines = []
    for s in spans:
        parent = f" parent={s['parent_span_id']}" if s["parent_span_id"] else " [root]"
        lines.append(
            f"  {s['service']}/{s['name']} "
            f"duration={s['duration_ms']}ms status={s['status']}{parent}"
        )

    if metadata_headers:
        header = f"[{TOOL_NAME}] trace_id={trace_id}\n"
        header += f"[{len(spans)} spans | sorted by duration desc]\n\n"
        output = header + "\n".join(lines)
    else:
        if not lines:
            return f"No spans found for trace {trace_id}."
        output = f"Trace {trace_id} ({len(spans)} spans):\n" + "\n".join(lines)

    if len(output) > TOOL_OUTPUT_MAX_CHARS:
        output = output[:TOOL_OUTPUT_MAX_CHARS] + f"\n\n[truncated -- {len(output)} chars total]"

    return output


def _attr_value(val: dict) -> str:
    """Extract a value from OTLP's typed attribute format.

    OTLP uses typed wrappers like {"stringValue": "x"} instead of just "x".
    This unwraps the value to a plain string for display.
    """
    for key in ("stringValue", "intValue", "boolValue", "doubleValue"):
        if key in val:
            return str(val[key])
    if "arrayValue" in val:
        items = val["arrayValue"].get("values", [])
        return str([_attr_value(v) for v in items])
    return str(val)


def _enrich_error(query: str, error: str) -> str:
    """Suggest fixes when a trace query fails (syntax errors, no results)."""
    hints = []
    error_lower = error.lower()
    if "parse error" in error_lower or "syntax" in error_lower:
        hints.append("[hint] Check TraceQL syntax. Example: {resource.service.name=\"payment\" && status=error}")
    try:
        r = requests.get(f"{TEMPO_URL}/api/search/tags", timeout=10)
        tags = r.json().get("tagNames", [])
        if tags:
            hints.append(f"[available tag keys] {', '.join(tags[:15])}")
    except Exception:
        pass
    if not hints:
        hints.append("[hint] Try broadening the search: remove filters or use service_name parameter instead of TraceQL")
    return "\n".join(hints)
