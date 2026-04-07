"""Step 2 of the data pipeline: generate diverse benchmark scenarios from real data.

Pipeline: seed_failures.py → curate_benchmarks.py

This script:
  1. Reads the time windows from seed_timestamps.json (created by seed_failures.py)
  2. For each failure window, queries Loki/Mimir/Tempo for actual signal data
  3. Feeds the real data + a baseline (healthy period) to an LLM
  4. The LLM generates N diverse scenarios per failure, varying:
     - Perspective (end-user, SRE, alert, manager)
     - Difficulty (easy=names the service, hard=vague symptom)
     - Entry point (different places in the failure cascade)
  5. Outputs eval/benchmark_dev.json and eval/benchmark_holdout.json

Why LLM curation instead of templates?
  - Scenarios are grounded in real data (only describes what signals actually show)
  - Diversity prevents overfitting to one phrasing style
  - Easy to generate more scenarios by increasing --scenarios-per-failure

Usage:
  python3 infra/curate_benchmarks.py                          # reads .env
  python3 infra/curate_benchmarks.py --model gpt-4o-mini      # cheaper model
  python3 infra/curate_benchmarks.py --scenarios-per-failure 5 # more scenarios
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

# Load .env from project root (one level up from infra/)
_env_path = Path(__file__).parent.parent / ".env"
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())

from openai import OpenAI


LOKI_URL = "http://localhost:3100"
TEMPO_URL = "http://localhost:3200"
PROM_URL = "http://localhost:9090"


# ---------------------------------------------------------------------------
# Data extraction: pull real signals from LGTM for a failure time window.
# Each function queries one backend and returns structured data that gets
# fed into the LLM curation prompt as context.
# ---------------------------------------------------------------------------

def query_loki_errors(start: str, end: str, limit: int = 100) -> list[dict]:
    """Get error/failure logs from all services in the window."""
    r = requests.get(f"{LOKI_URL}/loki/api/v1/query_range", params={
        "query": '{service_name=~".+"} |~ "(?i)(error|fail|exception|timeout|refused|panic|crash)"',
        "start": start,
        "end": end,
        "limit": limit,
        "direction": "backward",
    }, timeout=15)
    r.raise_for_status()
    results = r.json().get("data", {}).get("result", [])

    entries = []
    for stream in results:
        svc = stream.get("stream", {}).get("service_name", "unknown")
        for ts, log in stream.get("values", []):
            entries.append({"service": svc, "log": log[:300]})
    return entries


def query_loki_all(start: str, end: str, limit: int = 50) -> list[dict]:
    """Get ALL logs (not just errors) to see normal vs abnormal patterns."""
    r = requests.get(f"{LOKI_URL}/loki/api/v1/query_range", params={
        "query": '{service_name=~".+"}',
        "start": start,
        "end": end,
        "limit": limit,
        "direction": "backward",
    }, timeout=15)
    r.raise_for_status()
    results = r.json().get("data", {}).get("result", [])

    by_service = {}
    for stream in results:
        svc = stream.get("stream", {}).get("service_name", "unknown")
        count = len(stream.get("values", []))
        by_service[svc] = by_service.get(svc, 0) + count
    return by_service


def query_tempo_errors(start_epoch: int, end_epoch: int, limit: int = 30) -> list[dict]:
    """Get error traces from Tempo."""
    r = requests.get(f"{TEMPO_URL}/api/search", params={
        "start": start_epoch,
        "end": end_epoch,
        "limit": limit,
        "tags": "error=true",
    }, timeout=15)
    r.raise_for_status()
    traces = r.json().get("traces", [])

    return [{
        "rootService": t.get("rootServiceName", "?"),
        "rootOperation": t.get("rootTraceName", "?"),
        "durationMs": t.get("durationMs", 0),
        "spanCount": t.get("spanSets", [{}])[0].get("matched", 0) if t.get("spanSets") else 0,
    } for t in traces]


def query_tempo_all(start_epoch: int, end_epoch: int, limit: int = 20) -> list[dict]:
    """Get all traces (including successful) for baseline comparison."""
    r = requests.get(f"{TEMPO_URL}/api/search", params={
        "start": start_epoch,
        "end": end_epoch,
        "limit": limit,
    }, timeout=15)
    r.raise_for_status()
    traces = r.json().get("traces", [])

    return [{
        "rootService": t.get("rootServiceName", "?"),
        "rootOperation": t.get("rootTraceName", "?"),
        "durationMs": t.get("durationMs", 0),
    } for t in traces]


def query_tempo_trace_detail(start_epoch: int, end_epoch: int) -> list[dict]:
    """Get a sample error trace and drill into its spans."""
    r = requests.get(f"{TEMPO_URL}/api/search", params={
        "start": start_epoch,
        "end": end_epoch,
        "limit": 1,
        "tags": "error=true",
    }, timeout=15)
    r.raise_for_status()
    traces = r.json().get("traces", [])
    if not traces:
        return []

    trace_id = traces[0].get("traceID", "")
    if not trace_id:
        return []

    r2 = requests.get(
        f"{TEMPO_URL}/api/traces/{trace_id}",
        headers={"Accept": "application/json"},
        timeout=15,
    )
    r2.raise_for_status()
    data = r2.json()

    spans = []
    for batch in data.get("batches", []):
        svc_name = "unknown"
        for attr in batch.get("resource", {}).get("attributes", []):
            if attr.get("key") == "service.name":
                svc_name = attr.get("value", {}).get("stringValue", "unknown")

        for scope_spans in batch.get("scopeSpans", []):
            for span in scope_spans.get("spans", []):
                duration_ns = int(span.get("endTimeUnixNano", 0)) - int(span.get("startTimeUnixNano", 0))
                status_code = span.get("status", {}).get("code", 0)
                spans.append({
                    "service": svc_name,
                    "name": span.get("name", "?"),
                    "duration_ms": round(duration_ns / 1_000_000, 1),
                    "status": "ERROR" if status_code == 2 else "OK",
                    "kind": span.get("kind", 0),
                })

    return sorted(spans, key=lambda s: -s["duration_ms"])[:15]


def query_prometheus_signals(mid_epoch: int) -> dict:
    """Query various metrics at the midpoint of the failure window."""
    queries = {
        "http_error_rate_by_service": (
            'sum by (service_name) (rate(http_server_duration_seconds_count{http_response_status_code=~"5.."}[5m]))'
        ),
        "http_request_rate_by_service": (
            'sum by (service_name) (rate(http_server_duration_seconds_count[5m]))'
        ),
        "http_p99_latency_by_service": (
            'histogram_quantile(0.99, sum by (service_name, le) (rate(http_server_duration_seconds_bucket[5m])))'
        ),
        "rpc_error_rate_by_service": (
            'sum by (service_name) (rate(rpc_server_duration_milliseconds_count{rpc_grpc_status_code!="0",rpc_grpc_status_code!=""}[5m]))'
        ),
        "rpc_request_rate_by_service": (
            'sum by (service_name) (rate(rpc_server_duration_milliseconds_count[5m]))'
        ),
        "kafka_consumer_lag": (
            'kafka_consumer_fetch_latency_avg'
        ),
        "process_cpu_by_service": (
            'sum by (service_name) (rate(process_cpu_seconds_total[5m]))'
        ),
    }

    results = {}
    for name, query in queries.items():
        try:
            r = requests.get(f"{PROM_URL}/api/v1/query", params={
                "query": query,
                "time": mid_epoch,
            }, timeout=10)
            r.raise_for_status()
            data = r.json().get("data", {}).get("result", [])
            # Only include non-zero results
            non_zero = [
                {
                    "service": m.get("metric", {}).get("service_name", m.get("metric", {}).get("job", "?")),
                    "value": round(float(m["value"][1]), 4),
                }
                for m in data
                if float(m["value"][1]) > 0.0001
            ]
            if non_zero:
                results[name] = sorted(non_zero, key=lambda x: -x["value"])[:8]
        except Exception:
            pass

    return results


def extract_failure_data(record: dict) -> dict:
    """Extract all available signal data for a single failure window.

    Queries all three backends (Loki, Tempo, Prometheus) to build a complete
    picture of what the failure looks like in telemetry. This data is what
    the LLM sees when generating benchmark scenarios.
    """
    start = record["start"]
    end = record["end"]
    s_epoch = int(datetime.fromisoformat(start).timestamp())
    e_epoch = int(datetime.fromisoformat(end).timestamp())
    mid_epoch = (s_epoch + e_epoch) // 2

    return {
        "flag": record["flag"],
        "root_cause_service": record["root_cause_service"],
        "difficulty": record["difficulty"],
        "description": record["description"],
        "window": {"start": start, "end": end},
        "signals": {
            "error_logs": query_loki_errors(start, end, limit=80),
            "log_volume_by_service": query_loki_all(start, end, limit=200),
            "error_traces": query_tempo_errors(s_epoch, e_epoch, limit=30),
            "all_traces_sample": query_tempo_all(s_epoch, e_epoch, limit=15),
            "error_trace_detail": query_tempo_trace_detail(s_epoch, e_epoch),
            "metrics": query_prometheus_signals(mid_epoch),
        },
    }


def extract_baseline_data() -> dict:
    """Extract signal data from a healthy window (5 min before first failure).

    The baseline gives the LLM a comparison point so it can describe symptoms
    as deviations from normal behavior (e.g., "error rate spiked from 0.1% to 15%").
    """
    with open(Path(__file__).parent / "seed_timestamps.json") as f:
        data = json.load(f)

    # Use the 5 minutes before the first dev failure
    first = data["dev"][0]
    end = first["start"]
    e_epoch = int(datetime.fromisoformat(end).timestamp())
    s_epoch = e_epoch - 300  # 5 minutes before
    start = datetime.fromtimestamp(s_epoch, tz=timezone.utc).isoformat()

    return {
        "window": {"start": start, "end": end},
        "signals": {
            "error_logs": query_loki_errors(start, end, limit=30),
            "log_volume_by_service": query_loki_all(start, end, limit=100),
            "all_traces_sample": query_tempo_all(s_epoch, e_epoch, limit=15),
            "metrics": query_prometheus_signals((s_epoch + e_epoch) // 2),
        },
    }


# ---------------------------------------------------------------------------
# LLM curation: feed real signal data to an LLM to generate diverse scenarios.
# The prompt asks the LLM to vary perspective, difficulty, and cascade entry
# point, grounded in what the actual telemetry data shows.
# ---------------------------------------------------------------------------

CURATION_PROMPT = """You are generating benchmark scenarios for an incident investigation CLI agent.

The agent has access to Loki (logs), Prometheus (metrics), and Tempo (traces) for an e-commerce microservices system (the OpenTelemetry demo). The agent receives a symptom description and must investigate to find the root cause.

## Your task

Given the ACTUAL observability data from a failure window, generate {n} diverse benchmark scenarios. Each scenario describes the same underlying failure but from a different angle, specificity level, or user perspective.

## Failure data

**Flag toggled:** {flag}
**Actual root cause service:** {root_cause}
**Failure description:** {description}
**Time window:** {start} to {end}

### Error logs found
{error_logs}

### Log volume by service (higher volume may indicate a problem)
{log_volume}

### Error traces
{error_traces}

### Sample error trace spans (deepest detail)
{trace_detail}

### All traces sample (including healthy)
{all_traces}

### Prometheus metrics during failure
{metrics}

### Baseline (healthy period) for comparison
{baseline}

## Requirements for generated scenarios

1. **Vary the perspective**: end-user complaint, SRE alert, monitoring dashboard observation, manager escalation
2. **Vary specificity**: some symptoms should name the affected service (easy), others should describe user-facing impact vaguely (hard)
3. **Vary what's emphasized**: some mention errors, some mention latency, some mention missing functionality
4. **Ground in the data**: only describe symptoms that the actual data supports. If there are no error traces, don't say "traces show errors"
5. **Include cascading effects**: if the failure cascades (e.g., payment failure → checkout failure → load-generator errors), create scenarios that start from different points in the cascade
6. **Assign difficulty**: easy (names the service or very specific), medium (describes the area but not the exact service), hard (vague symptom that requires investigation)

## Output format

Return a JSON array. Each element:
```json
{{
  "id": "unique-id",
  "symptom": "The symptom description an SRE would receive",
  "expected_root_cause": "{root_cause}",
  "expected_signal": "What specific signal proves this diagnosis (grounded in the actual data above)",
  "difficulty": "easy|medium|hard",
  "perspective": "end-user|sre|alert|manager"
}}
```

Return a JSON object with a single key "scenarios" containing an array of exactly {n} scenario objects. Example: {{"scenarios": [{{...}}, {{...}}]}}"""


def curate_scenarios(
    client: OpenAI,
    failure_data: dict,
    baseline: dict,
    model: str,
    n: int,
) -> list[dict]:
    """Use an LLM to generate diverse benchmark scenarios from real signal data."""

    def fmt_logs(logs: list) -> str:
        if not logs:
            return "(no error logs found)"
        by_svc = {}
        for entry in logs:
            svc = entry["service"]
            by_svc.setdefault(svc, []).append(entry["log"])
        lines = []
        for svc, entries in sorted(by_svc.items(), key=lambda x: -len(x[1])):
            lines.append(f"  {svc}: {len(entries)} error logs")
            for log in entries[:3]:
                lines.append(f"    - {log[:200]}")
        return "\n".join(lines)

    def fmt_traces(traces: list) -> str:
        if not traces:
            return "(no error traces found)"
        by_svc = {}
        for t in traces:
            svc = t["rootService"]
            by_svc.setdefault(svc, []).append(t)
        lines = []
        for svc, ts in sorted(by_svc.items(), key=lambda x: -len(x[1])):
            ops = set(t["rootOperation"] for t in ts)
            durations = [t["durationMs"] for t in ts]
            avg_dur = sum(durations) / len(durations) if durations else 0
            lines.append(f"  {svc}: {len(ts)} error traces, ops={ops}, avg_duration={avg_dur:.0f}ms")
        return "\n".join(lines)

    def fmt_trace_detail(spans: list) -> str:
        if not spans:
            return "(no trace detail available)"
        lines = []
        for s in spans[:10]:
            status = "ERROR" if s["status"] == "ERROR" else "OK"
            lines.append(f"  [{status}] {s['service']}: {s['name']} ({s['duration_ms']}ms)")
        return "\n".join(lines)

    def fmt_metrics(metrics: dict) -> str:
        if not metrics:
            return "(no relevant metrics found)"
        lines = []
        for name, entries in metrics.items():
            lines.append(f"  {name}:")
            for e in entries[:5]:
                lines.append(f"    {e['service']}: {e['value']}")
        return "\n".join(lines)

    def fmt_log_volume(volume: dict) -> str:
        if not volume:
            return "(no log data)"
        lines = []
        for svc, count in sorted(volume.items(), key=lambda x: -x[1]):
            lines.append(f"  {svc}: {count} logs")
        return "\n".join(lines)

    fd = failure_data
    prompt = CURATION_PROMPT.format(
        n=n,
        flag=fd["flag"],
        root_cause=fd["root_cause_service"],
        description=fd["description"],
        start=fd["window"]["start"][:19],
        end=fd["window"]["end"][:19],
        error_logs=fmt_logs(fd["signals"]["error_logs"]),
        log_volume=fmt_log_volume(fd["signals"]["log_volume_by_service"]),
        error_traces=fmt_traces(fd["signals"]["error_traces"]),
        trace_detail=fmt_trace_detail(fd["signals"]["error_trace_detail"]),
        all_traces=fmt_traces(fd["signals"]["all_traces_sample"]),
        metrics=fmt_metrics(fd["signals"]["metrics"]),
        baseline=fmt_metrics(baseline["signals"]["metrics"]) + "\n" + fmt_log_volume(baseline["signals"]["log_volume_by_service"]),
    )

    is_reasoning = "5.4" in model or "o1" in model or "o3" in model or "o4" in model

    kwargs = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
    }
    if is_reasoning:
        kwargs["reasoning_effort"] = "low"
    else:
        kwargs["temperature"] = 0.7

    response = client.chat.completions.create(**kwargs)
    content = response.choices[0].message.content
    if not content:
        print(f"    [warn] Empty response from {model}")
        print(f"    [warn] finish_reason={response.choices[0].finish_reason}")
        return []

    parsed = json.loads(content)

    # Handle various LLM response shapes (different models format JSON differently):
    #   [...] — bare array (most common)
    #   {"scenarios": [...]} — wrapped array (requested format)
    #   {"id": ..., "symptom": ...} — single scenario object (gpt-5.4 does this sometimes)
    if isinstance(parsed, list):
        scenarios = parsed
    elif isinstance(parsed, dict):
        # Check if this is a single scenario (has "symptom" or "id" key)
        if "symptom" in parsed or "id" in parsed:
            scenarios = [parsed]
        else:
            # Look for a nested list
            for key in ("scenarios", "benchmarks"):
                if key in parsed and isinstance(parsed[key], list):
                    scenarios = parsed[key]
                    break
            else:
                for v in parsed.values():
                    if isinstance(v, list):
                        scenarios = v
                        break
                else:
                    scenarios = []
    else:
        scenarios = []

    # Ensure every scenario is a dict (not a string)
    valid = [s for s in scenarios if isinstance(s, dict)]
    if not valid and scenarios:
        print(f"    [warn] Got {len(scenarios)} items but none are dicts: {type(scenarios[0])}")
    if not valid and isinstance(parsed, dict):
        print(f"    [warn] Response keys: {list(parsed.keys())}")
        print(f"    [warn] First 200 chars: {content[:200]}")

    return valid


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Curate benchmarks from LGTM data using LLM")
    parser.add_argument("--model", default="gpt-4o", help="LLM model for curation")
    parser.add_argument("--scenarios-per-failure", type=int, default=4, help="Scenarios to generate per failure type")
    parser.add_argument(
        "--timestamps",
        type=Path,
        default=Path(__file__).parent / "seed_timestamps.json",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).parent.parent / "eval",
    )
    args = parser.parse_args()

    if not args.timestamps.exists():
        print(f"Error: {args.timestamps} not found. Run seed_failures.py first.")
        sys.exit(1)

    client = OpenAI()

    with open(args.timestamps) as f:
        ts_data = json.load(f)

    print("Extracting baseline (healthy) data...")
    baseline = extract_baseline_data()

    for round_name in ["dev", "holdout"]:
        records = ts_data.get(round_name, [])
        if not records:
            continue

        print(f"\n=== {round_name} round ===")
        all_scenarios = []

        for record in records:
            print(f"\n  Extracting signals for {record['flag']}...")
            failure_data = extract_failure_data(record)

            # Summarize what was found
            sigs = failure_data["signals"]
            print(f"    Logs: {len(sigs['error_logs'])} errors, {len(sigs['log_volume_by_service'])} services")
            print(f"    Traces: {len(sigs['error_traces'])} errors, {len(sigs['all_traces_sample'])} total")
            print(f"    Metrics: {len(sigs['metrics'])} signal groups")

            print(f"  Curating {args.scenarios_per_failure} scenarios with {args.model}...")
            scenarios = curate_scenarios(
                client, failure_data, baseline, args.model, args.scenarios_per_failure
            )

            # Attach time window to each scenario
            for s in scenarios:
                s["time_window"] = {
                    "start": record["start"],
                    "end": record["end"],
                }
                # Ensure expected_root_cause matches the actual service name
                s["expected_root_cause"] = record["root_cause_service"]

            all_scenarios.extend(scenarios)
            print(f"    Generated {len(scenarios)} scenarios")
            for s in scenarios:
                print(f"      [{s['difficulty']}] {s['id']}: {s['symptom'][:80]}...")

        # Write benchmark file
        output_path = args.output_dir / f"benchmark_{round_name}.json"
        args.output_dir.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(all_scenarios, f, indent=2)
            f.write("\n")

        print(f"\n  Wrote {len(all_scenarios)} scenarios to {output_path}")

    print("\nDone. Benchmark files ready for eval.")


if __name__ == "__main__":
    main()
