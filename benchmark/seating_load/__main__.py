"""CLI entrypoint for the seating load tests.

Usage (server must already be running via ``make run-e2e``):

    uv run python -m benchmark.seating_load --scenario all
    uv run python -m benchmark.seating_load --scenario storm --seed 42
    uv run python -m benchmark.seating_load --scenario poll --log-path /path/to/run-e2e.log

Exits non-zero if any hard assertion fails (soft latency targets never fail the run).
"""

import argparse
import sys
import typing as t

from .harness import DEFAULT_BASE_URL, LoadClient, LogWatcher, ScenarioResult, setup_django


def main() -> int:
    """Parse args, run the requested scenarios, print the final verdict."""
    parser = argparse.ArgumentParser(description="Seating engine HTTP load tests")
    parser.add_argument(
        "--scenario",
        choices=["storm", "herd", "poll", "purchase", "probes", "sweep", "all"],
        default="all",
        help="Which scenario to run (default: all, in order)",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--seed", type=int, default=1337, help="Deterministic seed for seat/party selection")
    parser.add_argument("--log-path", default=None, help="gunicorn log to scan for tracebacks after each scenario")
    args = parser.parse_args()

    setup_django()
    # Imported after setup_django(): scenarios pulls in Django models at call time.
    from .scenarios import (
        invariant_sweep,
        prepare_fixtures,
        scenario_availability_polling,
        scenario_best_available_herd,
        scenario_hold_storm,
        scenario_probes,
        scenario_purchase_race,
    )

    watcher = LogWatcher(args.log_path) if args.log_path else None
    client = LoadClient(args.base_url)
    print(f"Target: {args.base_url}  seed={args.seed}")

    if args.scenario == "all":
        order = ["storm", "herd", "poll", "purchase", "probes"]
    elif args.scenario == "sweep":
        order = []  # read-only ORM sweep: no HTTP scenarios, no fixture mutations
    else:
        order = [args.scenario]

    runners: dict[str, t.Callable[[], ScenarioResult]] = {}
    if order:
        fixtures = prepare_fixtures()
        runners = {
            "storm": lambda: scenario_hold_storm(client, fixtures, args.seed),
            "herd": lambda: scenario_best_available_herd(client, fixtures, args.seed),
            "poll": lambda: scenario_availability_polling(client, fixtures, args.seed),
            "purchase": lambda: scenario_purchase_race(client, fixtures, args.seed),
            "probes": lambda: scenario_probes(client, fixtures, args.seed),
        }

    results: list[ScenarioResult] = []
    log_errors: dict[str, list[str]] = {}
    try:
        for name in order:
            result = runners[name]()
            if watcher:
                log_errors[name] = watcher.report(name)
                if log_errors[name]:
                    result.passed = False
                    result.notes.append(f"FAIL: {len(log_errors[name])} new gunicorn error line(s)")
            results.append(result)
        if args.scenario in ("all", "sweep"):
            results.append(invariant_sweep())
    finally:
        client.close()

    print("\n=== Verdict ===")
    all_passed = True
    for res in results:
        hard_fail = not res.passed
        all_passed &= not hard_fail
        print(f"  {'PASS' if res.passed else 'FAIL'}  {res.name}: {res.notes[0] if res.notes else ''}")
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
