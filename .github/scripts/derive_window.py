"""Derive how large a watchdog window can safely be.

Both the reading schedule and the watchdog's CHECK_WINDOW_HOURS depend on the
cron geometry, so they can drift apart silently. This module is the shared
derivation: it simulates the guard's own rule and reports the gap distribution,
so the wiring check can assert a relationship instead of trusting a magic
number that someone typed in.

Background, from measuring the real system rather than reading the code:
GitHub's scheduler was running roughly 5h47m late, but that offset is very
consistent -- the same slot fired 6h13m late one day and 6h15m late the next.
A constant offset cancels out when you difference two fire times, so it does NOT
affect the gap between credited runs. Only per-slot *jitter* matters. That
distinction is the whole reason the window is ~26h and not ~32h.

Run directly for a readable report:

    python .github/scripts/derive_window.py
"""

import datetime
import random

# Defaults mirror auto-reading.yml. Callers should pass the real values.
DEFAULT_MIN_HOURS = 20
DEFAULT_SLOT_HOURS_UTC = (2, 8, 20)
JITTER_HOURS = 2.0
TRIALS = 2000
SEED = 20261002


def slot_fire_times(base_utc, days, jitter_hours, rng):
    """Every slot instant, each delayed by a random amount in [0, jitter]."""
    out = []
    day = base_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    for d in range(days + 1):
        for hour in DEFAULT_SLOT_HOURS_UTC:
            delay = rng.uniform(0, jitter_hours) if jitter_hours else 0.0
            out.append(day + datetime.timedelta(days=d, hours=hour + delay))
    return sorted(out)


def credited_gaps(slots, min_hours):
    """Gaps between consecutive credited runs under the guard's rule."""
    last = None
    gaps = []
    for when in slots:
        if last is None:
            last = when
            continue
        if (when - last).total_seconds() / 3600.0 >= min_hours:
            gaps.append((when - last).total_seconds() / 3600.0)
            last = when
    return gaps


def analyze(min_hours=DEFAULT_MIN_HOURS, jitter_hours=JITTER_HOURS,
            trials=TRIALS, seed=SEED, days=8):
    base = datetime.datetime(2026, 10, 2, tzinfo=datetime.timezone.utc)
    rng = random.Random(seed)
    observed = []
    for _ in range(trials):
        gaps = credited_gaps(
            slot_fire_times(base, days, jitter_hours, rng), min_hours
        )
        observed.extend(gaps)
    observed.sort()
    return {
        "count": len(observed),
        "median_h": observed[len(observed) // 2] if observed else None,
        "p99_h": observed[int(len(observed) * 0.99)] if observed else None,
        "max_h": max(observed) if observed else None,
        "min_hours": min_hours,
        "jitter_hours": jitter_hours,
        "trials": trials,
        "seed": seed,
    }


def main():
    print("WeRead reading job — watchdog window derivation")
    print()
    beijing = [f"{(h + 8) % 24:02d}:13" for h in DEFAULT_SLOT_HOURS_UTC]
    print(f"scheduled slots (UTC hours): {list(DEFAULT_SLOT_HOURS_UTC)}")
    print(f"  target Beijing times      : {', '.join(beijing)}")
    print(f"guard rule                 : run only if >= {DEFAULT_MIN_HOURS}h since last success")
    print()
    print("The observed GitHub delay is a near-constant offset, so it cancels out")
    print("of these gaps. Only jitter widens them.")
    print()
    header = f"{'jitter':>8}  {'median':>7}  {'p99':>7}  {'max':>7}"
    print(header)
    print("-" * len(header))
    for jitter in (0, 0.5, 1, 2, 3, 5):
        stats = analyze(jitter_hours=jitter)
        print(
            f"  0..{jitter:>4}h  {stats['median_h']:>7.1f}  "
            f"{stats['p99_h']:>7.1f}  {stats['max_h']:>7.1f}"
        )
    print()
    stats = analyze()
    print(f"at the assumed {JITTER_HOURS}h jitter, the widest normal gap is "
          f"{stats['max_h']:.1f}h")
    print("so CHECK_WINDOW_HOURS must sit above that (no false alarm) while")
    print("staying low enough that a fully silent failure is caught within a day.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())