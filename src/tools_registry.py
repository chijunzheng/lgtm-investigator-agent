import json
from tools import query_logs, query_metrics, query_traces, list_services

_REGISTRY = {
    "query_logs": query_logs,
    "query_metrics": query_metrics,
    "query_traces": query_traces,
    "list_services": list_services,
}

TOOL_DEFINITIONS = [mod.DEFINITION for mod in _REGISTRY.values()]


def dispatch(name: str, arguments: str, metadata_headers: bool = False, error_enrichment: bool = False) -> str:
    module = _REGISTRY.get(name)
    if not module:
        return f"Unknown tool: {name}"
    try:
        kwargs = json.loads(arguments) if arguments else {}
        return module.execute(metadata_headers=metadata_headers, error_enrichment=error_enrichment, **kwargs)
    except Exception as e:
        return f"Tool error ({type(e).__name__}): {e}"
