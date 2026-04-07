#!/usr/bin/env python3
"""Evaluation framework for benchmarking the investigate agent.

Runs curated incident scenarios against the agent, then scores the results
using three complementary methods:

1. **Hit detection** (_check_hit):  Binary — did the agent name the correct
   root cause service? Simple keyword match in the diagnosis block.

2. **LLM judge** (judge_diagnosis): Nuanced 1-5 ratings on three dimensions:
   root_cause_accuracy, evidence_quality, reasoning_quality.
   Uses chain-of-thought prompting for consistent scoring.

3. **Trace scoring** (score_path_from_trace): Objective metrics extracted
   directly from the execution trace: tool call count, signal coverage,
   self-corrections, cache hits, etc.

Usage:
  python3 eval/eval.py --version v2 --split dev          # Run V2 on dev split
  python3 eval/eval.py --version v3 --split multiturn    # Multi-turn eval
  python3 eval/eval.py --compare --split dev              # Compare all versions
  python3 eval/eval.py --version v2 --summary             # Print saved results
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from statistics import mean, median

# Allow imports from src/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import openai
from config import JUDGE_MODEL as _DEFAULT_JUDGE_MODEL
from agent import Agent
from prompts.system_v1 import SYSTEM_V1
from prompts.system_v2 import SYSTEM_V2
from judge_prompt import DIAGNOSIS_JUDGE_PROMPT

EVAL_DIR = Path(__file__).resolve().parent
RESULTS_DIR = EVAL_DIR / "results"
JUDGE_MODEL = _DEFAULT_JUDGE_MODEL

VERSION_CONFIGS = {
    "v1": {
        "system_prompt": SYSTEM_V1,
        "tools_enabled": True,
        "context_management": False,
        "tool_metadata_headers": False,
        "error_enrichment": False,
        "parallel_tool_calls": False,
        "inject_topology": False,
    },
    "v2": {
        "system_prompt": SYSTEM_V2,
        "tools_enabled": True,
        "context_management": False,
        "tool_metadata_headers": True,
        "error_enrichment": True,
        "parallel_tool_calls": True,
        "inject_topology": True,
    },
    "v3": {
        "system_prompt": SYSTEM_V2,
        "tools_enabled": True,
        "context_management": True,
        "tool_metadata_headers": True,
        "error_enrichment": True,
        "parallel_tool_calls": True,
        "inject_topology": True,
        "model_routing": False,
    },
    "v4": {
        "system_prompt": SYSTEM_V2,
        "tools_enabled": True,
        "context_management": True,
        "tool_metadata_headers": True,
        "error_enrichment": True,
        "parallel_tool_calls": True,
        "inject_topology": True,
        "model_routing": True,
    },
}


# ---------------------------------------------------------------------------
# Trace scoring — objective metrics from the execution trace
# ---------------------------------------------------------------------------

def score_path_from_trace(trace: list) -> dict:
    """Score investigation quality directly from the tool call trace.

    These metrics are objective (no LLM judgment needed):
      - total_tool_calls:       How many tools the agent called
      - signals_checked:        How many of {logs, metrics, traces} were queried
      - used_all_3_signals:     Whether the agent checked all three signal types
      - repeated_queries:       Wasted calls (same tool + same args)
      - cache_hits:             Queries served from the dedup cache (V3/V4)
      - self_corrected_queries: Times the agent retried a failed tool call and succeeded
      - failed_tool_calls:      Total tool errors (bad queries, connection failures)
    """
    tool_calls = [t for t in trace if t["type"] == "tool_call"]
    tools_used = [t["tool"] for t in tool_calls]

    signal_map = {
        "query_logs": "logs",
        "query_metrics": "metrics",
        "query_traces": "traces",
    }
    signals = set(signal_map.get(t, "other") for t in tools_used)

    unique_queries = set(f"{t['tool']}:{t['args']}" for t in tool_calls)

    self_corrected = 0
    for i, tc in enumerate(tool_calls):
        if tc.get("error") and i + 1 < len(tool_calls):
            next_tc = tool_calls[i + 1]
            if next_tc["tool"] == tc["tool"] and not next_tc.get("error"):
                self_corrected += 1

    return {
        "total_tool_calls": len(tool_calls),
        "unique_tools_used": len(set(tools_used)),
        "signals_checked": len(signals - {"other"}),
        "used_all_3_signals": signals >= {"logs", "metrics", "traces"},
        "repeated_queries": len(tool_calls) - len(unique_queries),
        "cache_hits": len([t for t in tool_calls if t.get("cached")]),
        "self_corrected_queries": self_corrected,
        "failed_tool_calls": len([t for t in tool_calls if t.get("error")]),
    }


# ---------------------------------------------------------------------------
# LLM Judge — uses a separate LLM to rate diagnosis quality
# ---------------------------------------------------------------------------

def judge_diagnosis(client, scenario: dict, response: str, n: int = 1) -> dict:
    """Run LLM judge n times, return median scores.

    The judge receives the scenario context (symptom, expected root cause) and
    the agent's full response, then rates quality on three 1-5 dimensions.
    Uses JSON response format for reliable parsing.

    Running n>1 times and taking the median reduces judge variance, but costs
    more. Default n=1 is used in practice (judge consistency is high enough).
    """
    scores = []
    for attempt in range(n):
        for retry in range(5):
            try:
                result = client.chat.completions.create(
                    model=JUDGE_MODEL,
                    messages=[{
                        "role": "user",
                        "content": DIAGNOSIS_JUDGE_PROMPT.format(
                            symptom=scenario["symptom"],
                            expected_root_cause=scenario["expected_root_cause"],
                            expected_signal=scenario["expected_signal"],
                            agent_response=response[-3000:],
                        ),
                    }],
                    temperature=0.3,
                    response_format={"type": "json_object"},
                )
                parsed = json.loads(result.choices[0].message.content)
                scores.append(parsed)
                break
            except openai.RateLimitError:
                wait = 2 ** retry * 15
                print(f"    [judge] rate limited, waiting {wait}s (retry {retry+1}/5)...")
                time.sleep(wait)
            except Exception as e:
                print(f"    [judge] attempt {attempt + 1} failed: {e}")
                break

    if not scores:
        return {"root_cause_accuracy": 1, "evidence_quality": 1, "reasoning_quality": 1}

    return {
        k: sorted([s[k] for s in scores])[len(scores) // 2]
        for k in ["root_cause_accuracy", "evidence_quality", "reasoning_quality"]
    }


# ---------------------------------------------------------------------------
# Hit detection — binary pass/fail on root cause identification
# ---------------------------------------------------------------------------

def _check_hit(response: str, expected_root_cause: str) -> bool:
    """Check if the agent identified the correct root cause in its diagnosis.

    Looks for the expected service name in the diagnosis conclusion,
    not the full response. Falls back to the last 500 chars if no
    'Root Cause:' block is found.
    """
    lower = response.lower()
    target = expected_root_cause.lower()

    # Try to extract the diagnosis block
    if "root cause:" in lower:
        # Get text after the LAST "Root Cause:" (the final diagnosis)
        diagnosis = lower.split("root cause:")[-1][:300]
        return target in diagnosis

    # Fallback: check the last 500 chars (likely the conclusion)
    return target in lower[-500:]


# ---------------------------------------------------------------------------
# Combined scoring — merges all three scoring methods into one result dict
# ---------------------------------------------------------------------------

def score_scenario(client, scenario, response, agent_stats, latency):
    """Combine hit detection + LLM judge + trace scoring into a single result."""
    return {
        "scenario_id": scenario["id"],
        "difficulty": scenario["difficulty"],
        "perspective": scenario.get("perspective", "unknown"),
        "expected_root_cause": scenario["expected_root_cause"],

        "hit": _check_hit(response, scenario["expected_root_cause"]),

        **judge_diagnosis(client, scenario, response),

        **score_path_from_trace(agent_stats["trace"]),

        **agent_stats["cost"],
        "latency_seconds": round(latency, 1),
    }


# ---------------------------------------------------------------------------
# Single-turn eval — one symptom per scenario, agent investigates once
# ---------------------------------------------------------------------------

def run_eval(version: str, split: str, limit: int = None) -> list:
    """Run all scenarios for a version+split. Returns list of scored results.

    For each scenario: create a fresh agent → run investigation → judge result.
    Results are saved incrementally (after each scenario) so partial runs
    are preserved if the process crashes or is interrupted.
    """
    config = VERSION_CONFIGS[version]
    benchmark_path = EVAL_DIR / f"benchmark_{split}.json"

    with open(benchmark_path) as f:
        scenarios = json.load(f)

    if limit:
        scenarios = scenarios[:limit]

    client = openai.OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    results = []
    total = len(scenarios)

    print(f"\n{'='*60}")
    print(f"  EVAL: {version} / {split} / {total} scenarios")
    print(f"{'='*60}\n")

    for i, scenario in enumerate(scenarios):
        if i > 0:
            time.sleep(5)  # rate limit buffer between scenarios
        print(f"[{i+1}/{total}] {scenario['id']} ({scenario['difficulty']})")

        agent = Agent(
            client=client,
            system_prompt=config["system_prompt"],
            tools_enabled=config["tools_enabled"],
            context_management_enabled=config["context_management"],
            tool_metadata_headers=config["tool_metadata_headers"],
            error_enrichment=config["error_enrichment"],
            parallel_tool_calls=config.get("parallel_tool_calls", False),
            inject_topology=config.get("inject_topology", False),
            model_routing=config.get("model_routing", False),
        )

        t0 = time.time()
        try:
            response = agent.run(scenario["symptom"], time_window=scenario["time_window"])
        except Exception as e:
            print(f"  [error] agent failed: {e}")
            response = f"Agent error: {e}"
        latency = time.time() - t0

        stats = agent.get_stats()

        print(f"  Judging... ", end="", flush=True)
        result = score_scenario(client, scenario, response, stats, latency)
        result["response_preview"] = response[:500]
        result["response_full"] = response
        result["response_tail_3k"] = response[-3000:]

        hit_str = "HIT" if result["hit"] else "MISS"
        print(f"{hit_str} | RCA={result['root_cause_accuracy']} "
              f"EV={result['evidence_quality']} "
              f"RE={result['reasoning_quality']} | "
              f"{result['total_tool_calls']} calls | "
              f"${result['estimated_cost']:.4f} | {result['latency_seconds']}s")

        results.append(result)

        # Save after every scenario so progress is visible
        RESULTS_DIR.mkdir(exist_ok=True)
        out_path = RESULTS_DIR / f"{version}_{split}.json"
        with open(out_path, "w") as f:
            json.dump(results, f, indent=2)

    print(f"\nResults saved to {out_path}")

    return results


# ---------------------------------------------------------------------------
# Multi-turn eval — agent keeps context across follow-up questions
# ---------------------------------------------------------------------------

def run_multiturn_eval(version: str, limit: int = None) -> list:
    """Run multi-turn scenarios. Agent is NOT reset between turns.

    This tests context management: can the agent handle follow-up questions
    without getting confused by accumulating tool results? V3's micro-compact
    is designed specifically to handle this scenario.
    """
    config = VERSION_CONFIGS[version]
    benchmark_path = EVAL_DIR / "benchmark_multiturn_dev.json"

    with open(benchmark_path) as f:
        scenarios = json.load(f)

    if limit:
        scenarios = scenarios[:limit]

    client = openai.OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    results = []
    total = len(scenarios)

    print(f"\n{'='*60}")
    print(f"  MULTI-TURN EVAL: {version} / {total} scenarios")
    print(f"{'='*60}\n")

    for i, scenario in enumerate(scenarios):
        if i > 0:
            time.sleep(5)
        print(f"[{i+1}/{total}] {scenario['id']} ({len(scenario['turns'])} turns)")

        agent = Agent(
            client=client,
            system_prompt=config["system_prompt"],
            tools_enabled=config["tools_enabled"],
            context_management_enabled=config["context_management"],
            tool_metadata_headers=config["tool_metadata_headers"],
            error_enrichment=config["error_enrichment"],
            parallel_tool_calls=config.get("parallel_tool_calls", False),
            inject_topology=config.get("inject_topology", False),
            model_routing=config.get("model_routing", False),
        )

        turn_results = []
        scenario_t0 = time.time()

        for turn_idx, turn in enumerate(scenario["turns"]):
            print(f"  Turn {turn_idx+1}/{len(scenario['turns'])}: {turn['symptom'][:60]}...")

            t0 = time.time()
            try:
                response = agent.run(turn["symptom"], time_window=scenario["time_window"])
            except Exception as e:
                print(f"    [error] agent failed: {e}")
                response = f"Agent error: {e}"
            latency = time.time() - t0

            stats = agent.get_stats()

            # Score this turn if it has an expected root cause
            if turn.get("expected_root_cause"):
                turn_scenario = {
                    "id": f"{scenario['id']}_turn{turn_idx+1}",
                    "symptom": turn["symptom"],
                    "expected_root_cause": turn["expected_root_cause"],
                    "expected_signal": turn.get("expected_signal", ""),
                    "difficulty": turn.get("difficulty", "medium"),
                }
                print(f"    Judging... ", end="", flush=True)
                scored = score_scenario(client, turn_scenario, response, stats, latency)
                hit_str = "HIT" if scored["hit"] else "MISS"
                print(f"{hit_str} | RCA={scored['root_cause_accuracy']} | "
                      f"{stats['total_tool_calls']} total calls | "
                      f"ctx: {stats['context_tokens']:,} tok")
            else:
                scored = {
                    "scenario_id": f"{scenario['id']}_turn{turn_idx+1}",
                    "difficulty": turn.get("difficulty", "medium"),
                    "hit": None,
                    "latency_seconds": round(latency, 1),
                }
                print(f"    [no expected root cause — skipping judge]")
                print(f"    {stats['total_tool_calls']} total calls | "
                      f"ctx: {stats['context_tokens']:,} tok")

            scored["turn"] = turn_idx + 1
            scored["symptom"] = turn["symptom"]
            scored["response_full"] = response
            scored["response_tail_3k"] = response[-3000:]
            scored["context_tokens_at_turn"] = stats["context_tokens"]
            scored["total_tool_calls_cumulative"] = stats["total_tool_calls"]
            scored["total_cost_cumulative"] = stats["cost"]["estimated_cost"]
            scored["cost_at_turn"] = stats["cost"]
            scored["cache_hits_cumulative"] = stats["cache_hits"]
            scored["micro_compacted_cumulative"] = stats["micro_compacted"]
            scored["trace"] = stats["trace"]
            turn_results.append(scored)

            time.sleep(15)  # rate limit buffer between turns

        total_latency = time.time() - scenario_t0

        scenario_result = {
            "scenario_id": scenario["id"],
            "num_turns": len(scenario["turns"]),
            "total_latency": round(total_latency, 1),
            "turns": turn_results,
            "final_context_tokens": turn_results[-1]["context_tokens_at_turn"],
            "final_cost": turn_results[-1]["total_cost_cumulative"],
            "final_tool_calls": turn_results[-1]["total_tool_calls_cumulative"],
            "final_cache_hits": turn_results[-1]["cache_hits_cumulative"],
            "final_micro_compacted": turn_results[-1]["micro_compacted_cumulative"],
        }
        results.append(scenario_result)

        # Save incrementally
        RESULTS_DIR.mkdir(exist_ok=True)
        out_path = RESULTS_DIR / f"{version}_multiturn.json"
        with open(out_path, "w") as f:
            json.dump(results, f, indent=2)

    print(f"\nResults saved to {out_path}")
    return results


def print_multiturn_summary(results: list, version: str):
    """Print multi-turn summary."""
    n = len(results)
    print(f"\n{'='*60}")
    print(f"  {version.upper()} / multiturn — {n} scenarios")
    print(f"{'='*60}")

    # Per-turn token growth
    print(f"\n  CONTEXT GROWTH (avg tokens at each turn)")
    max_turns = max(r["num_turns"] for r in results)
    for t in range(max_turns):
        tokens = [r["turns"][t]["context_tokens_at_turn"]
                  for r in results if t < len(r["turns"])]
        if tokens:
            print(f"    Turn {t+1}: {mean(tokens):,.0f} tokens (n={len(tokens)})")

    # Aggregate stats
    print(f"\n  AGGREGATE")
    print(f"    Avg final tokens:    {mean(r['final_context_tokens'] for r in results):,.0f}")
    print(f"    Avg final cost:      ${mean(r['final_cost'] for r in results):.4f}")
    print(f"    Avg tool calls:      {mean(r['final_tool_calls'] for r in results):.1f}")
    print(f"    Total cache hits:    {sum(r['final_cache_hits'] for r in results)}")
    print(f"    Total compacted:     {sum(r['final_micro_compacted'] for r in results)}")

    # Per-turn diagnosis (for scored turns only)
    scored_turns = [t for r in results for t in r["turns"] if t.get("hit") is not None]
    if scored_turns:
        hits = sum(1 for t in scored_turns if t["hit"])
        print(f"\n  DIAGNOSIS (scored turns only)")
        print(f"    Hit rate:          {hits}/{len(scored_turns)} ({hits/len(scored_turns)*100:.0f}%)")
        rca_vals = [t["root_cause_accuracy"] for t in scored_turns if "root_cause_accuracy" in t]
        ev_vals = [t["evidence_quality"] for t in scored_turns if "evidence_quality" in t]
        re_vals = [t["reasoning_quality"] for t in scored_turns if "reasoning_quality" in t]
        if rca_vals:
            print(f"    RCA score (1-5):   {mean(rca_vals):.2f}")
        if ev_vals:
            print(f"    Evidence (1-5):    {mean(ev_vals):.2f}")
        if re_vals:
            print(f"    Reasoning (1-5):   {mean(re_vals):.2f}")

    print(f"{'='*60}\n")


# ---------------------------------------------------------------------------
# Summary table
# ---------------------------------------------------------------------------

def print_summary(results: list, version: str, split: str):
    """Print summary table for a single version+split."""
    n = len(results)
    hits = sum(1 for r in results if r["hit"])

    by_difficulty = {}
    for r in results:
        d = r["difficulty"]
        by_difficulty.setdefault(d, []).append(r)

    by_root_cause = {}
    for r in results:
        rc = r["expected_root_cause"]
        by_root_cause.setdefault(rc, []).append(r)

    by_perspective = {}
    for r in results:
        p = r.get("perspective", "unknown")
        by_perspective.setdefault(p, []).append(r)

    def avg(key):
        vals = [r[key] for r in results if key in r]
        return mean(vals) if vals else 0

    print(f"\n{'='*60}")
    print(f"  {version.upper()} / {split} — {n} scenarios")
    print(f"{'='*60}")

    print(f"\n  DIAGNOSIS")
    print(f"    Hit rate:          {hits}/{n} ({hits/n*100:.0f}%)")
    print(f"    RCA score (1-5):   {avg('root_cause_accuracy'):.2f}")
    print(f"    Evidence (1-5):    {avg('evidence_quality'):.2f}")
    print(f"    Reasoning (1-5):   {avg('reasoning_quality'):.2f}")

    print(f"\n  BY DIFFICULTY")
    for d in ["easy", "medium", "hard"]:
        group = by_difficulty.get(d, [])
        h = sum(1 for r in group if r["hit"])
        print(f"    {d:8s} hit rate:  {h}/{len(group)}")

    print(f"\n  BY ROOT CAUSE")
    for rc in sorted(by_root_cause.keys()):
        group = by_root_cause[rc]
        h = sum(1 for r in group if r["hit"])
        print(f"    {rc:20s} {h}/{len(group)}")

    print(f"\n  BY PERSPECTIVE")
    for p in sorted(by_perspective.keys()):
        group = by_perspective[p]
        h = sum(1 for r in group if r["hit"])
        print(f"    {p:12s} hit rate:  {h}/{len(group)}")

    print(f"\n  INVESTIGATION")
    print(f"    Avg tool calls:    {avg('total_tool_calls'):.1f}")
    print(f"    Avg signals:       {avg('signals_checked'):.1f}")
    all3 = sum(1 for r in results if r.get("used_all_3_signals"))
    print(f"    All 3 signals:     {all3}/{n}")
    print(f"    Repeated queries:  {avg('repeated_queries'):.1f}")
    print(f"    Failed tool calls: {avg('failed_tool_calls'):.1f}")
    print(f"    Self-corrected:    {avg('self_corrected_queries'):.1f}")
    print(f"    Cache hits:        {avg('cache_hits'):.1f}")

    print(f"\n  COST")
    print(f"    Avg tokens:        {avg('input_tokens') + avg('output_tokens'):,.0f}")
    print(f"    Avg cost:          ${avg('estimated_cost'):.4f}")
    print(f"    Avg latency:       {avg('latency_seconds'):.1f}s")

    print(f"{'='*60}\n")


# ---------------------------------------------------------------------------
# Compare versions
# ---------------------------------------------------------------------------

def compare_versions(split: str = "dev"):
    """Load saved results for all versions and print comparison table."""
    versions = {}
    for v in ["v1", "v2", "v3"]:
        path = RESULTS_DIR / f"{v}_{split}.json"
        if path.exists():
            with open(path) as f:
                versions[v] = json.load(f)

    if not versions:
        print("No results found. Run evals first.")
        return

    def col(results, key):
        vals = [r[key] for r in results if key in r]
        return mean(vals) if vals else 0

    header = f"{'':25s}"
    for v in versions:
        header += f"{v.upper():>12s}"
    print(f"\n{header}")
    print("-" * (25 + 12 * len(versions)))

    rows = [
        ("DIAGNOSIS", None),
        ("  Hit rate", lambda rs: f"{sum(1 for r in rs if r['hit'])}/{len(rs)}"),
        ("  RCA score (1-5)", lambda rs: f"{col(rs, 'root_cause_accuracy'):.2f}"),
        ("  Evidence (1-5)", lambda rs: f"{col(rs, 'evidence_quality'):.2f}"),
        ("  Reasoning (1-5)", lambda rs: f"{col(rs, 'reasoning_quality'):.2f}"),
        ("", None),
        ("INVESTIGATION", None),
        ("  Avg tool calls", lambda rs: f"{col(rs, 'total_tool_calls'):.1f}"),
        ("  Signals checked", lambda rs: f"{col(rs, 'signals_checked'):.1f}"),
        ("  All 3 signals", lambda rs: f"{sum(1 for r in rs if r.get('used_all_3_signals'))}/{len(rs)}"),
        ("  Repeated queries", lambda rs: f"{col(rs, 'repeated_queries'):.1f}"),
        ("  Failed tool calls", lambda rs: f"{col(rs, 'failed_tool_calls'):.1f}"),
        ("  Self-corrected", lambda rs: f"{col(rs, 'self_corrected_queries'):.1f}"),
        ("  Cache hits", lambda rs: f"{col(rs, 'cache_hits'):.1f}"),
        ("", None),
        ("COST", None),
        ("  Avg tokens", lambda rs: f"{col(rs, 'input_tokens') + col(rs, 'output_tokens'):,.0f}"),
        ("  Avg cost", lambda rs: f"${col(rs, 'estimated_cost'):.4f}"),
        ("  Avg latency", lambda rs: f"{col(rs, 'latency_seconds'):.1f}s"),
    ]

    for label, fn in rows:
        if fn is None:
            print(f"\n{label}")
            continue
        line = f"  {label:23s}"
        for v in versions:
            line += f"{fn(versions[v]):>12s}"
        print(line)

    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Investigate CLI Eval")
    parser.add_argument("--version", choices=["v1", "v2", "v3", "v4"], help="Version to eval")
    parser.add_argument("--split", choices=["dev", "holdout", "multiturn", "balanced"], default="balanced", help="Benchmark split")
    parser.add_argument("--limit", type=int, help="Limit number of scenarios (for testing)")
    parser.add_argument("--judge-model", type=str, help="Override judge model (e.g., gpt-4.1)")
    parser.add_argument("--compare", action="store_true", help="Compare saved results")
    parser.add_argument("--all", action="store_true", help="Run all versions x both splits")
    parser.add_argument("--summary", action="store_true", help="Print summary of saved results")
    args = parser.parse_args()

    if args.judge_model:
        global JUDGE_MODEL
        JUDGE_MODEL = args.judge_model

    if args.compare:
        compare_versions(args.split)
        return

    if args.summary:
        if args.split == "multiturn":
            path = RESULTS_DIR / f"{args.version}_multiturn.json"
            if path.exists():
                with open(path) as f:
                    results = json.load(f)
                print_multiturn_summary(results, args.version)
            else:
                print(f"No results at {path}")
        else:
            path = RESULTS_DIR / f"{args.version}_{args.split}.json"
            if path.exists():
                with open(path) as f:
                    results = json.load(f)
                print_summary(results, args.version, args.split)
            else:
                print(f"No results at {path}")
        return

    if args.all:
        for v in VERSION_CONFIGS:
            for s in ["dev", "holdout"]:
                results = run_eval(v, s)
                print_summary(results, v, s)
        compare_versions("dev")
        compare_versions("holdout")
        return

    if args.version:
        if args.split == "multiturn":
            results = run_multiturn_eval(args.version, limit=args.limit)
            print_multiturn_summary(results, args.version)
        else:
            results = run_eval(args.version, args.split, limit=args.limit)
            print_summary(results, args.version, args.split)
        return

    parser.print_help()


if __name__ == "__main__":
    main()
