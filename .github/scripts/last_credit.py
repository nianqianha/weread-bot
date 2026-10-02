"""Find the most recent scheduled run that actually credited reading time.

Shared by the reading job's guard step and the hourly watchdog, because they
must agree. Both need the answer to "has a session actually banked any reading
time recently?", and the obvious proxies are all wrong:

  * `conclusion == "success"` -- during the inverted-gate incident four runs
    reported success while doing nothing at all, because the bot step was
    skipped and a skipped step still counts as a successful run.
  * "the run uploaded an artifact" -- the artifact upload is `if: always()`,
    so a run that failed during startup still uploads whatever log file
    existed, with no run-history.json inside.

The only trustworthy signal is the recorded total_duration_seconds inside the
run-history.json artifact. Verified against 14 real runs: artifact presence and
credited>0 disagreed twice, in both directions.

Prints the created_at of the newest credited scheduled run, or nothing.
Exit code is always 0: a failure here must never block the caller, so the
caller treats empty output as "unknown" and runs.
"""

import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.environ.get("REPO", "")
WORKFLOW = os.environ.get("WORKFLOW", "auto-reading.yml")
MAX_RUNS = int(os.environ.get("MAX_RUNS", "5"))


def gh(args, timeout=180):
    env = dict(os.environ)
    env.setdefault("GH_TOKEN", os.environ.get("GITHUB_TOKEN", ""))
    proc = subprocess.run(["gh", *args], capture_output=True, timeout=timeout, env=env)
    if proc.returncode != 0:
        raise RuntimeError(
            f"gh {' '.join(args)} failed: {proc.stderr.decode('utf-8', 'replace')[:160]}"
        )
    return proc.stdout


def credited_seconds(run_id):
    """Sum of total_duration_seconds, or 0 when the run banked nothing."""
    workdir = tempfile.mkdtemp(prefix=f"lc-{run_id}-")
    try:
        gh(["run", "download", str(run_id), "-R", REPO, "-D", workdir])
    except Exception:
        return 0
    total = 0
    try:
        for path in glob.glob(os.path.join(workdir, "**", "run-history.json"), recursive=True):
            with open(path, "r", encoding="utf-8") as handle:
                for record in json.load(handle):
                    total += int(record.get("total_duration_seconds") or 0)
    except Exception:
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return total


def latest_credit():
    runs = json.loads(
        gh(["api", f"repos/{REPO}/actions/workflows/{WORKFLOW}/runs?per_page=30"])
    ).get("workflow_runs", [])
    # manual dispatches are deliberately excluded: they must not consume the
    # daily budget, otherwise a manual test would suppress the nightly slot
    runs = [r for r in runs if r.get("event") == "schedule"]

    for run in runs[:MAX_RUNS]:
        seconds = credited_seconds(run["id"])
        print(
            f"  inspected run {run['id']} conclusion={run.get('conclusion')} "
            f"credited={seconds}s",
            file=sys.stderr,
        )
        if seconds > 0:
            return run["created_at"], seconds
    return None, 0


def main():
    if not REPO:
        print("")
        return 0
    try:
        created_at, seconds = latest_credit()
    except Exception as exc:  # noqa: BLE001
        print(f"last_credit: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("")
        return 0
    print(created_at or "")
    if created_at:
        print(f"  most recent credited session: {seconds}s", file=sys.stderr)
    else:
        print("  no credited scheduled session found in the recent window", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())