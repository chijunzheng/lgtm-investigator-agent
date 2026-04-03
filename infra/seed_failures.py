"""Toggle OTel demo feature flags to generate failure telemetry.

Runs two rounds:
  Round 1 -> dev set time windows
  Round 2 -> held-out set time windows

Same flags in both rounds. Different timestamps = different data.
Modifies the flagd config file directly (flagd watches and auto-reloads).

Usage:
  python3 infra/seed_failures.py
  python3 infra/seed_failures.py --flagd-config ./custom/path/demo.flagd.json
  python3 infra/seed_failures.py --failure-duration 120 --recovery-duration 60
"""
import argparse
import json
import sys
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

# Default path relative to this script's location
DEFAULT_FLAGD_CONFIG = (
    Path(__file__).parent / "opentelemetry-demo" / "src" / "flagd" / "demo.flagd.json"
)

# Each flag entry: flag name in flagd config, the variant that enables the failure,
# the variant that disables it, and metadata for benchmark generation.
#
# NOTE: These names match the actual OTel demo flagd config (v1.12+).
#   - paymentFailure: float variants (100%=1.0, off=0). NOT paymentServiceFailure.
#   - adHighCpu: bool (on/off). NOT adServiceHighCpu.
#   - kafkaQueueProblems: integer variants (on=100, off=0).
#   - productCatalogFailure: bool (on/off).
FLAGS = [
    {
        "flag": "paymentFailure",
        "on_variant": "100%",
        "off_variant": "off",
        "description": "All payment charges fail (Charge RPC returns error)",
        "root_cause_service": "payment",
        "difficulty": "easy",
    },
    {
        "flag": "productCatalogFailure",
        "on_variant": "on",
        "off_variant": "off",
        "description": "Product catalog fails on specific product ID",
        "root_cause_service": "product-catalog",
        "difficulty": "easy",
    },
    {
        "flag": "kafkaQueueProblems",
        "on_variant": "on",
        "off_variant": "off",
        "description": "Kafka queue overload causing consumer lag",
        "root_cause_service": "kafka",
        "difficulty": "medium",
    },
    {
        "flag": "adHighCpu",
        "on_variant": "on",
        "off_variant": "off",
        "description": "High CPU usage in ad service",
        "root_cause_service": "ad",
        "difficulty": "hard",
    },
]

FAILURE_DURATION = 300   # 5 min of failure data per flag
RECOVERY_DURATION = 120  # 2 min recovery between flags
ROUND_GAP = 300          # 5 min gap between dev and held-out rounds


def load_flagd_config(config_path: Path) -> dict:
    """Read the flagd config JSON file."""
    with open(config_path) as f:
        return json.load(f)


def save_flagd_config(config_path: Path, config: dict) -> None:
    """Write the flagd config JSON file. flagd auto-reloads on change."""
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")


def toggle_flag(config_path: Path, flag_name: str, variant: str) -> None:
    """Set a flag's defaultVariant and write the config.

    Creates a new config dict (immutable pattern) and writes it.
    flagd watches the file and picks up the change within ~1 second.
    """
    config = load_flagd_config(config_path)

    if flag_name not in config.get("flags", {}):
        print(f"  [warn] Flag '{flag_name}' not found in {config_path}")
        print(f"  [warn] Available flags: {list(config.get('flags', {}).keys())}")
        return

    flag = config["flags"][flag_name]
    if variant not in flag.get("variants", {}):
        print(f"  [warn] Variant '{variant}' not found for flag '{flag_name}'")
        print(f"  [warn] Available variants: {list(flag.get('variants', {}).keys())}")
        return

    updated_config = deepcopy(config)
    updated_config["flags"][flag_name]["defaultVariant"] = variant
    save_flagd_config(config_path, updated_config)

    # Brief pause for flagd to detect the file change
    time.sleep(2)


def verify_flagd_config(config_path: Path) -> bool:
    """Check that the flagd config file exists and contains expected flags."""
    if not config_path.exists():
        print(f"  [error] flagd config not found at: {config_path}")
        print(f"  [error] Run setup.sh first to clone the OTel demo.")
        return False

    config = load_flagd_config(config_path)
    flags = config.get("flags", {})

    missing = [f["flag"] for f in FLAGS if f["flag"] not in flags]
    if missing:
        print(f"  [warn] Missing flags in config: {missing}")
        print(f"  [warn] Available flags: {sorted(flags.keys())}")
        print(f"  [warn] The OTel demo version may have different flag names.")
        print(f"  [warn] Proceeding with available flags only.")

    return True


def seed_round(
    round_name: str,
    config_path: Path,
    failure_duration: int,
    recovery_duration: int,
) -> list[dict]:
    """Run one round of failure seeding.

    For each flag:
      1. Enable the failure
      2. Wait for failure_duration seconds (telemetry accumulates)
      3. Disable the failure
      4. Wait for recovery_duration seconds (baseline data between failures)
      5. Record the time window

    Returns list of records with timestamps for benchmark generation.
    """
    config = load_flagd_config(config_path)
    available_flags = config.get("flags", {})

    records = []
    for i, flag_def in enumerate(FLAGS):
        flag_name = flag_def["flag"]
        if flag_name not in available_flags:
            print(f"  [{round_name}] Skipping {flag_name} (not in config)")
            continue

        total = len([f for f in FLAGS if f["flag"] in available_flags])
        print(f"  [{round_name}] ({i+1}/{total}) Enabling {flag_name}...")

        start = datetime.now(timezone.utc).isoformat()
        toggle_flag(config_path, flag_name, flag_def["on_variant"])

        print(f"  [{round_name}] Failure active. Waiting {failure_duration}s for telemetry...")
        _countdown(failure_duration)

        end = datetime.now(timezone.utc).isoformat()
        toggle_flag(config_path, flag_name, flag_def["off_variant"])

        records.append({
            "round": round_name,
            "flag": flag_name,
            "root_cause_service": flag_def["root_cause_service"],
            "description": flag_def["description"],
            "difficulty": flag_def["difficulty"],
            "start": start,
            "end": end,
        })

        print(f"  [{round_name}] {flag_name} done. Recovering {recovery_duration}s...")
        _countdown(recovery_duration)

    return records


def _countdown(seconds: int) -> None:
    """Print a countdown timer on one line."""
    for remaining in range(seconds, 0, -30):
        mins, secs = divmod(remaining, 60)
        print(f"    {mins:02d}:{secs:02d} remaining...", end="\r", flush=True)
        time.sleep(min(30, remaining))
    print("    done.                    ")


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed failure scenarios for benchmarking")
    parser.add_argument(
        "--flagd-config",
        type=Path,
        default=DEFAULT_FLAGD_CONFIG,
        help=f"Path to demo.flagd.json (default: {DEFAULT_FLAGD_CONFIG})",
    )
    parser.add_argument(
        "--failure-duration",
        type=int,
        default=FAILURE_DURATION,
        help=f"Seconds to keep each failure active (default: {FAILURE_DURATION})",
    )
    parser.add_argument(
        "--recovery-duration",
        type=int,
        default=RECOVERY_DURATION,
        help=f"Seconds between failures (default: {RECOVERY_DURATION})",
    )
    parser.add_argument(
        "--round-gap",
        type=int,
        default=ROUND_GAP,
        help=f"Seconds between dev and holdout rounds (default: {ROUND_GAP})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).parent / "seed_timestamps.json",
        help="Output path for timestamps JSON",
    )
    args = parser.parse_args()

    if not verify_flagd_config(args.flagd_config):
        sys.exit(1)

    total_time = (
        2 * (len(FLAGS) * (args.failure_duration + args.recovery_duration))
        + args.round_gap
    )
    mins = total_time // 60
    print(f"=== Seeding failure scenarios (2 rounds) ===")
    print(f"    Estimated total time: ~{mins} minutes")
    print(f"    Failure duration: {args.failure_duration}s per flag")
    print(f"    Recovery duration: {args.recovery_duration}s between flags")
    print(f"    Round gap: {args.round_gap}s")
    print()

    print("Round 1: Dev set")
    dev_records = seed_round(
        "dev", args.flagd_config, args.failure_duration, args.recovery_duration
    )

    print(f"\nWaiting {args.round_gap}s between rounds...\n")
    _countdown(args.round_gap)

    print("Round 2: Held-out set")
    holdout_records = seed_round(
        "holdout", args.flagd_config, args.failure_duration, args.recovery_duration
    )

    all_records = {"dev": dev_records, "holdout": holdout_records}
    with open(args.output, "w") as f:
        json.dump(all_records, f, indent=2)
        f.write("\n")

    print(f"\nDone. Timestamps saved to {args.output}")
    print(f"Next: python3 infra/build_benchmarks.py")


if __name__ == "__main__":
    main()
