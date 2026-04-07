"""Central registry for all tools the agent can call.

Each tool is a Python module in src/tools/ that exports:
  - DEFINITION: OpenAI function-calling schema (name, description, parameters)
  - execute():  Function that calls the observability backend and returns a string

This registry provides:
  - TOOL_DEFINITIONS: List of all schemas (passed to OpenAI API as `tools=`)
  - dispatch():       Routes a tool call from the LLM to the correct module

The agent never calls tools directly — it always goes through dispatch().
"""

import json
from tools import query_logs, query_metrics, query_traces, list_services

# Map tool names (as the LLM sees them) to their implementing modules
_REGISTRY = {
    "query_logs": query_logs,       # Loki (LogQL)
    "query_metrics": query_metrics, # Mimir/Prometheus (PromQL)
    "query_traces": query_traces,   # Tempo (TraceQL)
    "list_services": list_services, # Aggregates service names from Tempo + Mimir
}

# Collected schemas for the OpenAI API tools= parameter
TOOL_DEFINITIONS = [mod.DEFINITION for mod in _REGISTRY.values()]


def dispatch(name: str, arguments: str, metadata_headers: bool = False, error_enrichment: bool = False) -> str:
    """Route a tool call to the correct module and return the result string.

    Args:
        name:             Tool name from the LLM's tool_call (e.g., "query_logs")
        arguments:        JSON string of arguments from the LLM
        metadata_headers: If True, prepend machine-readable context to the result
                          (result count, query echo, truncation status) — V2+ feature
        error_enrichment: If True, append hints when a query fails
                          (available labels, syntax examples) — V2+ feature

    Returns:
        The tool's output as a plain string (added to context as a tool message).
    """
    module = _REGISTRY.get(name)
    if not module:
        return f"Unknown tool: {name}"
    try:
        kwargs = json.loads(arguments) if arguments else {}
        return module.execute(metadata_headers=metadata_headers, error_enrichment=error_enrichment, **kwargs)
    except Exception as e:
        return f"Tool error ({type(e).__name__}): {e}"
