"""Generate benchmark JSON files from seed_timestamps.json.

Reads the timestamps recorded by seed_failures.py and produces:
  - eval/benchmark_dev.json     (4 scenarios for prompt iteration)
  - eval/benchmark_holdout.json (4 scenarios for final evaluation)

Same failure types, different time windows, reworded symptoms.

Usage:
  python3 infra/build_benchmarks.py
  python3 infra/build_benchmarks.py --timestamps infra/seed_timestamps.json
"""
import argparse
import json
import sys
from pathlib import Path

# Scenario templates: dev symptoms vs holdout symptoms (reworded).
# Keyed by flag name to match seed_timestamps.json records.
SCENARIOS = {
    "paymentFailure": {
        "dev": {
            "id": "dev-payment-failure",
            "symptom": (
                "Orders are failing at checkout. Customers see errors "
                "after clicking Place Order."
            ),
        },
        "holdout": {
            "id": "holdout-payment-failure",
            "symptom": (
                "Checkout is broken. Payment step fails for all users."
            ),
        },
        "expected_root_cause": "payment",
        "expected_signal": "error on Charge RPC call",
    },
    "productCatalogFailure": {
        "dev": {
            "id": "dev-catalog-failure",
            "symptom": (
                "Product pages are returning errors. Some products fail to load."
            ),
        },
        "holdout": {
            "id": "holdout-catalog-failure",
            "symptom": (
                "Certain product detail pages are broken. Browsing the catalog "
                "shows intermittent failures."
            ),
        },
        "expected_root_cause": "product-catalog",
        "expected_signal": "GetProduct RPC returning errors for specific product ID",
    },
    "kafkaQueueProblems": {
        "dev": {
            "id": "dev-kafka-lag",
            "symptom": (
                "Order confirmations are delayed. Some orders placed 10 minutes "
                "ago still show as processing."
            ),
        },
        "holdout": {
            "id": "holdout-kafka-lag",
            "symptom": (
                "Background processing seems stuck. Events that should be "
                "processed within seconds are taking minutes."
            ),
        },
        "expected_root_cause": "kafka",
        "expected_signal": "consumer lag spike or queue overload",
    },
    "adHighCpu": {
        "dev": {
            "id": "dev-ad-cpu",
            "symptom": (
                "Overall system performance degraded. Pages are slower than "
                "usual across the board."
            ),
        },
        "holdout": {
            "id": "holdout-ad-cpu",
            "symptom": (
                "Multiple services are responding slowly. No single service "
                "seems to be the obvious culprit."
            ),
        },
        "expected_root_cause": "ad",
        "expected_signal": "high CPU usage in adservice",
    },
}


def build_benchmark(records: list[dict], round_name: str) -> list[dict]:
    """Build a benchmark file from seed timestamp records for one round."""
    scenarios = []
    for record in records:
        flag = record["flag"]
        template = SCENARIOS.get(flag)
        if template is None:
            print(f"  [warn] No scenario template for flag '{flag}', skipping")
            continue

        round_template = template[round_name]
        scenarios.append({
            "id": round_template["id"],
            "symptom": round_template["symptom"],
            "time_window": {
                "start": record["start"],
                "end": record["end"],
            },
            "expected_root_cause": template["expected_root_cause"],
            "expected_signal": template["expected_signal"],
            "difficulty": record["difficulty"],
        })

    return scenarios


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate benchmark JSONs from seed timestamps")
    parser.add_argument(
        "--timestamps",
        type=Path,
        default=Path(__file__).parent / "seed_timestamps.json",
        help="Path to seed_timestamps.json",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).parent.parent / "eval",
        help="Directory for benchmark output files",
    )
    args = parser.parse_args()

    if not args.timestamps.exists():
        print(f"Error: {args.timestamps} not found.")
        print(f"Run seed_failures.py first to generate timestamps.")
        sys.exit(1)

    with open(args.timestamps) as f:
        data = json.load(f)

    args.output_dir.mkdir(parents=True, exist_ok=True)

    for round_name in ["dev", "holdout"]:
        records = data.get(round_name, [])
        if not records:
            print(f"  [warn] No records for round '{round_name}'")
            continue

        benchmark = build_benchmark(records, round_name)
        output_path = args.output_dir / f"benchmark_{round_name}.json"

        with open(output_path, "w") as f:
            json.dump(benchmark, f, indent=2)
            f.write("\n")

        print(f"  {output_path}: {len(benchmark)} scenarios")
        for scenario in benchmark:
            print(f"    - {scenario['id']} ({scenario['difficulty']}): {scenario['expected_root_cause']}")

    print(f"\nBenchmark files written to {args.output_dir}/")


if __name__ == "__main__":
    main()
