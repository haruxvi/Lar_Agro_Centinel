"""Measure Argon2id hashing cost and suggest parameters for this machine.

OWASP guidance and common practice put interactive password hashing in the
250-500 ms range: slow enough to make offline cracking expensive, fast enough
that a login does not feel stuck.

Usage:
    python scripts/benchmark_argon2.py [--samples 5]
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_PATH = REPO_ROOT / "backend"
if str(BACKEND_PATH) not in sys.path:
    sys.path.insert(0, str(BACKEND_PATH))

from argon2 import PasswordHasher  # noqa: E402

TARGET_MIN_MS = 250.0
TARGET_MAX_MS = 500.0
SAMPLE_PASSWORD = "benchmark-password-with-enough-length"  # noqa: S105


def measure(time_cost: int, memory_cost: int, parallelism: int, samples: int) -> float:
    """Return the median milliseconds a single hash takes."""
    hasher = PasswordHasher(
        time_cost=time_cost,
        memory_cost=memory_cost,
        parallelism=parallelism,
        hash_len=32,
        salt_len=16,
    )
    timings: list[float] = []
    for _ in range(samples):
        started = time.perf_counter()
        hasher.hash(SAMPLE_PASSWORD)
        timings.append((time.perf_counter() - started) * 1000)
    return statistics.median(timings)


def suggest(time_cost: int, memory_cost: int, parallelism: int, samples: int) -> None:
    """Print a parameter set landing inside the target window, if one is found."""
    for candidate_time_cost in range(time_cost, time_cost + 8):
        elapsed = measure(candidate_time_cost, memory_cost, parallelism, samples)
        if TARGET_MIN_MS <= elapsed <= TARGET_MAX_MS:
            print(
                f"\nSuggested: ARGON2_TIME_COST={candidate_time_cost} "
                f"ARGON2_MEMORY_COST={memory_cost} "
                f"ARGON2_PARALLELISM={parallelism}  ({elapsed:.0f} ms)"
            )
            return
        if elapsed > TARGET_MAX_MS:
            print(
                f"\ntime_cost={candidate_time_cost} already takes {elapsed:.0f} ms; "
                "lower memory_cost instead."
            )
            return
    print("\nNo parameter set inside the target window was found; raise memory_cost.")


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns the process exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=5, help="hashes per measurement")
    args = parser.parse_args(argv)

    from app.shared.config import get_settings

    settings = get_settings()
    time_cost = settings.argon2_time_cost
    memory_cost = settings.argon2_memory_cost
    parallelism = settings.argon2_parallelism

    print(
        f"Configured: time_cost={time_cost} "
        f"memory_cost={memory_cost} KiB ({memory_cost / 1024:.0f} MiB) "
        f"parallelism={parallelism}"
    )
    elapsed = measure(time_cost, memory_cost, parallelism, args.samples)
    print(f"Median hash time over {args.samples} samples: {elapsed:.0f} ms")

    if TARGET_MIN_MS <= elapsed <= TARGET_MAX_MS:
        print(f"Within the {TARGET_MIN_MS:.0f}-{TARGET_MAX_MS:.0f} ms target window.")
        return 0

    verdict = "faster" if elapsed < TARGET_MIN_MS else "slower"
    print(f"Outside the target window ({verdict} than intended).")
    suggest(time_cost, memory_cost, parallelism, args.samples)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
