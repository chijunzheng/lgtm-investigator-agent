# Investigator Agent

An AI-powered incident investigation agent that queries Loki (logs), Mimir (metrics), and Tempo (traces) to diagnose production issues. Built with OpenAI's function-calling API, with a Grafana plugin frontend and a comprehensive eval framework.

```
> Checkout attempts are failing. Customers see errors after clicking Place Order.

  [topology] injected service list
  [parallel] executing 3 tool calls concurrently
  [tool] query_logs({"query":"{service_name=\"checkout\"} |= \"error\"", ...})
  [tool] query_metrics({"query":"rate(rpc_server_duration_milliseconds_count{...}[5m])", ...})
  [tool] query_traces({"service_name":"checkout", ...})

  ──────────────────── Diagnosis ────────────────────

  Root Cause: payment -- Charge RPC returning INTERNAL error for all requests
  Confidence: HIGH
  Evidence:
    - Metrics: rpc error rate for payment spiked to 12.3 req/s (baseline: 0)
    - Traces: checkout → payment Charge span shows status=ERROR, duration=2ms
    - Logs: "PaymentService/Charge failed: credit card expired" across all requests
  Remediation: Check payment provider configuration or circuit-break the Charge RPC
```

## Architecture

```
                       ┌──────────────────────────────┐
                       │         Agent Loop            │
                       │   (think → act → observe)     │
                       │                               │
                       │   V1: baseline                │
                       │   V2: + prompt + parallel     │
                       │   V3: + context management    │
                       │   V4: + model routing         │
                       └───────────┬───────────────────┘
                                   │
             ┌─────────────────────┼──────────────────────┐
             │                     │                      │
       ┌─────┴──────┐      ┌──────┴───────┐      ┌───────┴──────┐
       │    Loki    │      │    Mimir     │      │    Tempo     │
       │   (logs)   │      │  (metrics)   │      │  (traces)    │
       │   LogQL    │      │   PromQL     │      │  TraceQL     │
       └────────────┘      └──────────────┘      └──────────────┘
             │                     │                      │
             └─────────────────────┼──────────────────────┘
                                   │
                   ┌───────────────┴────────────────┐
                   │     LGTM Docker Container      │
                   │   (pre-loaded failure data)     │
                   └────────────────────────────────┘
```

The agent loop is the same code for all versions -- differences are controlled by feature flags. The eval framework compares V1 vs V4 by flipping constructor arguments, not by running different code.

## Live Demo

Open the hosted instance -- no setup required:

```
http://34.121.52.184:3000/a/investigate-investigate-app
```

The Investigate plugin is in the left sidebar. Select a demo scenario or type any incident description to start an investigation.

## Demo Scenarios

The CLI ships with pre-loaded demo scenarios scoped to known failure windows:

```
> help
  Commands:
    demo            — list demo scenarios
    demo <name>     — run a demo scenario
    stats           — show tool calls, cost, tokens
    reset           — clear agent state
    quit            — exit

> demo
  Available demo scenarios:
    payment-failure           All payment charges fail (Charge RPC returns error)
    product-catalog-failure   Product catalog fails on specific product ID
    kafka-failure             Kafka queue overload causing consumer lag
    ad-failure                High CPU usage in ad service

> demo payment-failure
```

## Version Comparison

| Feature | V1 | V2 | V3 | V4 |
|---|:---:|:---:|:---:|:---:|
| System prompt | Basic | SRE methodology | = V2 | = V2 |
| Parallel tool calls | | yes | yes | yes |
| Metadata headers | | yes | yes | yes |
| Error enrichment | | yes | yes | yes |
| Topology injection | | yes | yes | yes |
| Context management | | | yes | yes |
| Model routing | | | | yes |

### Single-Turn Results (n=32, balanced split)

| Metric | V1 | V2 | V3 | V4 |
|---|---:|---:|---:|---:|
| Hit rate | 47% | 59% | 50% | 62% |
| Evidence quality (1-5) | 2.47 | 4.38 | 4.16 | 4.69 |
| Tool calls | 17.8 | 13.8 | 14.9 | 12.9 |
| Cost per investigation | $0.088 | $0.087 | $0.100 | $0.088 |
| Latency | 224s | 185s | 184s | 147s |

### Multi-Turn Results (n=8 scenarios, 17 scored turns)

| Metric | V1 | V2 | V3 | V4 |
|---|---:|---:|---:|---:|
| Hit rate | 59% | 53% | 76% | 82% |
| RCA score (1-5) | 2.71 | 1.94 | 3.71 | 3.12 |
| Evidence quality (1-5) | 2.65 | 3.00 | 4.53 | 4.35 |

V4 leads multi-turn at **82% hit rate** — combining V3's micro-compact (which jumps from 53% to 76% by clearing stale tool results) with model routing for lower cost per turn.

## Deploying to GCP

Deploy the full app (LGTM + agent + reverse proxy) to a single GCP VM:

```bash
# Prerequisites: gcloud CLI authenticated, OPENAI_API_KEY in .env
./infra/deploy-gcp.sh

# Output:
#   App URL: http://<EXTERNAL_IP>
#   Grafana: http://<EXTERNAL_IP>/grafana/  (admin/admin)
```

The deploy script creates a VM, uploads the data snapshot and source code, starts all services behind a Caddy reverse proxy, and opens the firewall. One URL, zero setup for the reviewer.

To tear down:

```bash
gcloud compute instances delete investigate-app --zone=us-central1-a --quiet
gcloud compute firewall-rules delete allow-investigate-app --quiet
```

## Running Evals

```bash
# Single version on dev split
python3 eval/eval.py --version v2 --split dev

# Multi-turn scenarios
python3 eval/eval.py --version v3 --split multiturn

# Compare all saved results
python3 eval/eval.py --compare --split dev

# Print summary
python3 eval/eval.py --version v2 --split dev --summary
```

## Project Structure

```
investigate-cli/
├── src/
│   ├── main.py              # CLI entry point + REPL
│   ├── agent.py             # Core agent loop (streaming, retry, tool execution)
│   ├── context.py           # Context management (micro-compact, query cache)
│   ├── config.py            # Environment config, model selection, limits
│   ├── cost_tracker.py      # Token usage + per-model cost tracking
│   ├── tools_registry.py    # Tool dispatch registry
│   ├── tools/
│   │   ├── query_logs.py    # Loki (LogQL)
│   │   ├── query_metrics.py # Mimir (PromQL)
│   │   ├── query_traces.py  # Tempo (TraceQL + span detail)
│   │   └── list_services.py # Service topology discovery
│   ├── prompts/
│   │   ├── system_v1.py     # Baseline prompt (3 lines)
│   │   └── system_v2.py     # SRE methodology + structured diagnosis
│   └── web/
│       └── server.py        # FastAPI + WebSocket for Grafana plugin
├── eval/
│   ├── eval.py              # Benchmark runner + LLM judge + trace scoring
│   ├── judge_prompt.py      # Chain-of-thought judge prompt
│   ├── run_all.sh           # Run all versions sequentially
│   ├── benchmark_balanced.json  # 32 LLM-curated single-turn scenarios
│   └── benchmark_multiturn_dev.json  # 8 multi-turn scenarios
├── infra/
│   ├── docker-compose.yml        # LGTM backend (local dev)
│   ├── docker-compose.deploy.yml # Full stack (LGTM + agent + proxy)
│   ├── deploy-gcp.sh            # One-click GCP deployment
│   ├── setup.sh                 # Full from-scratch setup
│   ├── seed_failures.py         # Toggle feature flags, record timestamps
│   ├── curate_benchmarks.py     # Generate benchmarks from real signal data
│   ├── export_data.sh           # Export LGTM data to tarball
│   └── import_data.sh           # Import tarball into Docker volume
├── grafana-plugin/              # Grafana app plugin (React + TypeScript)
├── requirements.txt
└── .env.example
```

## Key Design Decisions

**Context management (micro-compact):** Before every LLM call, old tool results are replaced with short markers like `[Cleared: query_logs(...)]`. The LLM's own reasoning summaries persist, so it can reference past findings without raw data cluttering the context. Inspired by Claude Code's approach to context management.

**Model routing (V4):** The first LLM call does a broad parallel sweep -- this doesn't need deep reasoning, so V4 uses a cheaper model (gpt-4.1). All subsequent calls use the full model (gpt-5.4). Result: 51% fewer tokens, 54% lower latency.

**Eval framework:** Three complementary scoring methods -- binary hit detection, LLM judge with chain-of-thought (1-5 ratings on three dimensions), and objective trace metrics. 40 benchmark scenarios (32 single-turn + 8 multi-turn) curated by an LLM from real telemetry data, ensuring diversity across perspectives, difficulty levels, and failure cascade entry points.
