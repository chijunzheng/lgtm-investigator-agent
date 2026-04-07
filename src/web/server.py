"""FastAPI WebSocket backend for the Grafana plugin.

Bridges the synchronous agent loop (blocking HTTP calls to Loki/Mimir/Tempo)
with the async Grafana plugin frontend via WebSocket streaming.

Architecture:
  1. Grafana plugin connects via WebSocket at /ws
  2. User sends {"type": "investigate", "symptom": "..."} message
  3. Agent runs in a background thread, emitting events to an asyncio.Queue
  4. Async loop drains the queue and sends events to WebSocket
  5. For tool results, we also synthesize "panel_add" events that tell the
     Grafana plugin which visualization panel to render (timeseries, logs, table)

Event types sent to the frontend:
  - reasoning:    LLM's thinking text (rendered as markdown)
  - tool_start:   Tool call initiated (shows loading state)
  - tool_result:  Tool call completed (raw result data)
  - panel_add:    Synthesized — tells Grafana to add a visualization panel
  - done:         Investigation complete (includes diagnosis and stats)
  - error:        Something went wrong
"""

import asyncio
import json
import os
import re

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

import openai
from agent import Agent
from config import MODEL
from prompts.system_v1 import SYSTEM_V1
from prompts.system_v2 import SYSTEM_V2

# Same version configs as main.py (see main.py for detailed comments on each version)
VERSION_CONFIGS = {
    "v1": {
        "system_prompt": SYSTEM_V1,
        "tools_enabled": True,
        "context_management_enabled": False,
        "tool_metadata_headers": False,
        "error_enrichment": False,
        "parallel_tool_calls": False,
        "inject_topology": False,
    },
    "v2": {
        "system_prompt": SYSTEM_V2,
        "tools_enabled": True,
        "context_management_enabled": False,
        "tool_metadata_headers": True,
        "error_enrichment": True,
        "parallel_tool_calls": True,
        "inject_topology": True,
    },
    "v3": {
        "system_prompt": SYSTEM_V2,
        "tools_enabled": True,
        "context_management_enabled": True,
        "tool_metadata_headers": True,
        "error_enrichment": True,
        "parallel_tool_calls": True,
        "inject_topology": True,
        "model_routing": False,
    },
    "v4": {
        "system_prompt": SYSTEM_V2,
        "tools_enabled": True,
        "context_management_enabled": True,
        "tool_metadata_headers": True,
        "error_enrichment": True,
        "parallel_tool_calls": True,
        "inject_topology": True,
        "model_routing": True,
    },
}

# Maps each tool to its Grafana visualization type and datasource UID.
# When a tool_result event arrives, we use this to synthesize a panel_add event
# that tells the Grafana plugin what kind of panel to render.
# UIDs (e.g., "prometheus") match the LGTM container's provisioned datasources.
TOOL_PANEL_MAP = {
    "query_metrics": ("timeseries", "prometheus"),
    "query_logs": ("logs", "loki"),
    "query_traces": ("table", "tempo"),
}

def _build_panel_title(tool_name: str, query: str, result_preview: str = "") -> str:
    """Build a human-readable panel title from tool name and query.

    Examples:
      query_logs + '{service_name="payment"} |= "error"' → "payment logs — \"error\""
      query_metrics + 'rate(http_server_duration_seconds_count{...}[5m])' → "Http Server Duration — payment"
      query_traces + '{resource.service.name="payment"}' → "payment traces"
    """
    if tool_name == "query_logs":
        service = re.search(r'service_name[=~]+"([^"]+)"', query)
        log_filter = re.search(r'\|=\s*"([^"]+)"', query)
        svc = service.group(1) if service else ""
        flt = log_filter.group(1) if log_filter else ""
        if svc and flt:
            return f"{svc} logs — \"{flt}\""
        if svc:
            return f"{svc} logs"
        return f"Logs: {query[:50]}"

    if tool_name == "query_metrics":
        metric = re.search(r'(\w+_\w+(?:_\w+)*)\s*[\[{(]', query)
        service = re.search(r'service_name[=~]+"([^"]+)"', query)
        m = metric.group(1) if metric else ""
        s = service.group(1) if service else ""
        if m:
            label = m.replace("_total", "").replace("_count", "").replace("_sum", "")
            label = label.replace("_seconds", "").replace("_milliseconds", " (ms)")
            label = label.replace("_", " ").title()
            if s:
                return f"{label} — {s}"
            return label
        return query[:50]

    if tool_name == "query_traces":
        service = re.search(r'service\.name\s*=\s*"([^"]+)"', query)
        if service:
            return f"{service.group(1)} traces"
        # Trace ID lookup — extract root span info from result preview
        if re.match(r'^[0-9a-f]{16,32}$', query, re.IGNORECASE):
            return _trace_title_from_preview(query, result_preview)
        return f"Traces: {query[:40]}"

    return query[:60]


def _trace_title_from_preview(trace_id: str, preview: str) -> str:
    """Build a descriptive trace title from the tool result preview.

    The preview contains span lines like:
      service/operationName duration=110.5ms status=OK [root]
    """
    if not preview:
        return f"Trace {trace_id[:16]}..."

    # Find the root span (marked with [root]) or take the first span line
    root_match = re.search(
        r'(\S+)/(\S+)\s+duration=([\d.]+)ms\s+status=(\S+)\s+\[root\]',
        preview,
    )
    if root_match:
        svc, op, dur, status = root_match.groups()
        dur_label = f"{float(dur):.0f}ms" if float(dur) < 1000 else f"{float(dur)/1000:.1f}s"
        return f"{svc} / {op} ({dur_label})"

    # Fallback: extract first span line
    first_span = re.search(r'(\S+)/(\S+)\s+duration=([\d.]+)ms', preview)
    if first_span:
        svc, op, dur = first_span.groups()
        dur_label = f"{float(dur):.0f}ms" if float(dur) < 1000 else f"{float(dur)/1000:.1f}s"
        return f"{svc} / {op} ({dur_label})"

    return f"Trace {trace_id[:16]}..."


app = FastAPI(title="Investigate API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _load_demo_scenarios():
    """Load demo scenarios from seed_timestamps.json."""
    from pathlib import Path
    ts_path = Path(__file__).resolve().parent.parent.parent / "infra" / "seed_timestamps.json"
    if not ts_path.exists():
        return []

    with open(ts_path) as f:
        data = json.load(f)

    symptom_map = {
        "paymentFailure": "Checkout attempts are failing, and the payment service may be rejecting charges. Please investigate the payment path.",
        "productCatalogFailure": "Some product pages are failing to load. Users are seeing errors when browsing the catalog.",
        "kafkaQueueProblems": "Order confirmations and emails are severely delayed. Something seems wrong with our async processing pipeline.",
        "adHighCpu": "The website is slow, especially pages with ads. Response times have spiked in the last few minutes.",
    }

    scenarios = []
    for window in data.get("dev", []):
        flag = window["flag"]
        symptom = symptom_map.get(flag, f"Investigate issues related to {window['root_cause_service']}")
        scenarios.append({
            "name": f"{window['root_cause_service']}-failure",
            "description": window["description"],
            "symptom": symptom,
            "time_window": {"start": window["start"], "end": window["end"]},
        })
    return scenarios


DEMO_SCENARIOS = _load_demo_scenarios()


@app.get("/api/demos")
def get_demos():
    return DEMO_SCENARIOS


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()

    version = os.getenv("AGENT_VERSION", "v2")
    config = VERSION_CONFIGS[version]
    api_key = os.getenv("OPENAI_API_KEY", "")
    client = openai.OpenAI(api_key=api_key)
    agent = None

    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)

            if msg["type"] == "reset":
                if agent:
                    agent.reset()
                agent = None
                await ws.send_json({"type": "reset_ack"})
                continue

            if msg["type"] == "investigate":
                symptom = msg.get("symptom", "")
                time_window = msg.get("time_window")

                # Create a fresh agent per investigation
                loop = asyncio.get_event_loop()

                def _result_has_data(preview: str) -> bool:
                    """Check whether a tool result preview indicates actual data was returned."""
                    if not preview:
                        return False
                    lower = preview.lower()
                    return not any(p in lower for p in (
                        "0 results", "no data", "no metric data found",
                        "no log lines found", "no traces found", "no spans found",
                        "no matching",
                    ))

                async def send_event(event: dict):
                    """Send agent event + synthesize panel_add events for tool results."""
                    await ws.send_json(event)

                    # When a tool completes, also send a panel_add if it maps to a viz
                    if event["type"] == "tool_result" and not event.get("error"):
                        panel_info = TOOL_PANEL_MAP.get(event["tool"])
                        if panel_info and event.get("query") and _result_has_data(event.get("result_preview", "")):
                            panel_type, ds_uid = panel_info
                            query = event["query"]
                            # Trace ID lookups → use 'traces' panel (waterfall view)
                            if event["tool"] == "query_traces" and re.match(r'^[0-9a-f]{16,32}$', query, re.IGNORECASE):
                                panel_type = "traces"
                            title = _build_panel_title(event["tool"], query, event.get("result_preview", ""))
                            await ws.send_json({
                                "type": "panel_add",
                                "panel_type": panel_type,
                                "title": title,
                                "datasource_uid": ds_uid,
                                "query": event["query"],
                                "time_from": time_window["start"] if time_window else "",
                                "time_to": time_window["end"] if time_window else "",
                            })

                # Bridge pattern: sync agent thread → async WebSocket
                # The agent runs in a thread (blocking HTTP calls), but WebSocket
                # sends are async. asyncio.Queue bridges the gap:
                #   agent thread → call_soon_threadsafe(queue.put) → async drain → ws.send
                pending_events = asyncio.Queue()

                def on_event(event: dict):
                    loop.call_soon_threadsafe(pending_events.put_nowait, event)

                agent = Agent(
                    client=client,
                    system_prompt=config["system_prompt"],
                    tools_enabled=config["tools_enabled"],
                    context_management_enabled=config["context_management_enabled"],
                    tool_metadata_headers=config["tool_metadata_headers"],
                    error_enrichment=config["error_enrichment"],
                    parallel_tool_calls=config["parallel_tool_calls"],
                    inject_topology=config["inject_topology"],
                    model_routing=config.get("model_routing", False),
                    on_event=on_event,
                )

                # Run agent in thread, drain events in async loop
                async def run_agent():
                    await asyncio.to_thread(agent.run, symptom, time_window)

                agent_task = asyncio.create_task(run_agent())

                # Drain events until agent finishes
                while not agent_task.done() or not pending_events.empty():
                    try:
                        event = await asyncio.wait_for(pending_events.get(), timeout=0.1)
                        await send_event(event)
                    except asyncio.TimeoutError:
                        continue

                # Ensure agent task completed cleanly
                await agent_task

    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await ws.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
