"""CLI entry point for the Investigate tool.

Two modes:
  python3 src/main.py --version v4          → interactive REPL (terminal)
  python3 src/main.py --version v4 --web    → FastAPI server (Grafana plugin)

The REPL supports:
  - Free-form incident descriptions ("Checkout is failing")
  - Demo scenarios with pre-loaded time windows ("demo payment-failure")
  - Stats, reset, and help commands
"""

import argparse
import os
import sys
import json
import readline
from pathlib import Path

# Allow running from project root: python3 src/main.py
sys.path.insert(0, str(Path(__file__).resolve().parent))

import requests
import openai
from config import LOKI_URL, MIMIR_URL, TEMPO_URL, MODEL
from agent import Agent
from prompts.system_v1 import SYSTEM_V1
from prompts.system_v2 import SYSTEM_V2

# ---------------------------------------------------------------------------
# Version configs: each version enables a different set of agent features.
# Same agent.py code, different feature flags → different behavior.
#
# V1: Baseline — minimal prompt, sequential tools, no enrichment
# V2: Enhanced prompt + parallel tools + metadata headers + error enrichment + topology
# V3: V2 + context management (micro-compact clears old tool results)
# V4: V3 + model routing (cheap model for first sweep call)
# ---------------------------------------------------------------------------
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

PROJECT_ROOT = Path(__file__).resolve().parent.parent

def _load_demo_scenarios():
    """Load pre-configured demo scenarios from infra/seed_timestamps.json.

    Each demo has a failure symptom and a time window scoped to when the
    failure was active (created by infra/seed_failures.py). This lets users
    quickly test the agent against known failures without typing timestamps.
    """
    ts_path = PROJECT_ROOT / "infra" / "seed_timestamps.json"
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


def check_health(name: str, url: str, path: str = "/ready") -> bool:
    """Verify an observability backend is reachable before starting the REPL."""
    try:
        r = requests.get(f"{url}{path}", timeout=5)
        if r.status_code < 400:
            print(f"  > {name} connected")
            return True
    except Exception:
        pass
    print(f"  > {name} UNREACHABLE ({url})")
    return False


def print_help():
    print("  Commands:")
    print("    demo            — list demo scenarios")
    print("    demo <name>     — run a demo scenario (e.g., demo payment-failure)")
    print("    stats           — show tool calls, cost, tokens")
    print("    reset           — clear agent state")
    print("    quit            — exit")
    print()
    print("  Or type any incident description to investigate.")


def print_demos():
    print()
    if not DEMO_SCENARIOS:
        print("  No demo scenarios found. Run infra/seed_failures.py first.")
        return
    print("  Available demo scenarios:")
    for s in DEMO_SCENARIOS:
        print(f"    {s['name']:25s} {s['description']}")
    print()
    print("  Usage: demo <name>")


def main():
    """Parse CLI args, check backends, create agent, and start the REPL loop."""
    parser = argparse.ArgumentParser(description="Investigate CLI", add_help=False)
    parser.add_argument("--version", choices=["v1", "v2", "v3", "v4"], default="v1")
    parser.add_argument("--web", action="store_true", help="Start web UI server instead of CLI")
    args = parser.parse_args()

    if args.web:
        from config import WEB_HOST, WEB_PORT
        import uvicorn
        os.environ.setdefault("AGENT_VERSION", args.version)
        print(f"\n  Starting web server (version {args.version.upper()})...")
        print(f"  API: http://{WEB_HOST}:{WEB_PORT}")
        print(f"  Open Grafana plugin at http://localhost:3001/grafana/a/investigate-investigate-app\n")
        uvicorn.run("web.server:app", host=WEB_HOST, port=WEB_PORT, reload=False)
        return

    version = args.version
    config = VERSION_CONFIGS[version]

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("Error: OPENAI_API_KEY not set.")
        print("  Add it to .env or: export OPENAI_API_KEY=sk-...")
        sys.exit(1)

    print()
    print("  Investigate CLI")
    print("  Terminal-first incident investigation")
    print()

    backends = [
        ("Loki", LOKI_URL, "/ready"),
        ("Mimir", MIMIR_URL, "/-/ready"),
        ("Tempo", TEMPO_URL, "/ready"),
    ]
    all_healthy = all(check_health(name, url, path) for name, url, path in backends)
    if not all_healthy:
        print("\n  Warning: some backends are unreachable. Queries may fail.")

    print(f"  Model: {MODEL}")
    print(f"  Version: {version.upper()}")
    print()
    print("  Type 'help' for commands, 'demo' for pre-loaded scenarios.")
    print()

    client = openai.OpenAI(api_key=api_key)
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
    )

    readline.parse_and_bind("tab: complete")

    while True:
        try:
            user_input = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break

        if not user_input:
            continue

        lower = user_input.lower()

        if lower in ("exit", "quit", "q"):
            print("Bye.")
            break

        if lower == "help":
            print_help()
            continue

        if lower == "demo":
            print_demos()
            continue

        if lower.startswith("demo "):
            name = lower.split(None, 1)[1]
            scenario = next((s for s in DEMO_SCENARIOS if s["name"] == name), None)
            if not scenario:
                print(f"  Unknown scenario: {name}")
                print_demos()
                continue
            print(f"\n  Running: {scenario['description']}")
            print(f"  Window:  {scenario['time_window']['start']} to {scenario['time_window']['end']}")
            print()
            agent.reset()
            agent.run(scenario["symptom"], time_window=scenario["time_window"])
            print()
            continue

        if lower == "stats":
            stats = agent.get_stats()
            print(f"  Tool calls: {stats['total_tool_calls']}")
            print(f"  LLM calls: {stats['total_llm_calls']}")
            print(f"  Cost: ${stats['cost']['estimated_cost']:.4f}")
            print(f"  Tokens: {stats['cost']['input_tokens'] + stats['cost']['output_tokens']:,}")
            continue

        if lower == "reset":
            agent.reset()
            print("  Agent state reset.")
            continue

        print()
        agent.run(user_input)
        print()


if __name__ == "__main__":
    main()
