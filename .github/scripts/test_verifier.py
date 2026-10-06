"""Prove the wiring verifier actually catches the bugs we hit in production.

A regression test that has never been shown to fail is not a regression test.
Each case below injects one real defect into a throwaway copy of the workspace
and asserts verify_config.py objects. Everything is restored on the way out, and
the fixed state is re-checked by case A at the start.

This file lives in the repo on purpose. Four separate times a day an assertion
was written loosely enough that an injected defect still passed:
  G  a comment mentioning the filename satisfied "guard calls the helper"
  J  the alert's display code satisfied "watchdog counts manual runs"
  K  a single-quoted comparison satisfied "no conclusion==success anywhere"
  L  the identifier survived while the behaviour was removed
A suite that only runs on someone's laptop protects nobody, so verify.yml runs
this on every push.

Set WORKSPACE to point at a different checkout; it defaults to the repository
root containing this file.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
# verify_config.py is a sibling in the repo layout, and is also a sibling in the
# flat scratch directory this is developed in -- so derive it by adjacency.
VERIFY = os.path.join(HERE, "verify_config.py")

# WORKSPACE defaults to the repository root two levels up (this file lives at
# <repo>/.github/scripts/). Fail loudly rather than walking a wrong tree.
WORKSPACE = os.environ.get("WORKSPACE") or os.path.dirname(os.path.dirname(HERE))
if not os.path.isdir(os.path.join(WORKSPACE, ".github", "scripts")):
    sys.exit(
        "test_verifier: cannot locate the workspace.\n"
        f"  verifier : {VERIFY}\n"
        f"  workspace: {WORKSPACE}\n"
        "Set WORKSPACE to the repository root (the directory containing .github/)."
    )
if not os.path.exists(VERIFY):
    sys.exit(f"test_verifier: verifier not found at {VERIFY}")

# Every case mutates a copy, never the caller's checkout: a failed assertion must
# not leave the tree dirty, and a crashed run must not either.
if os.path.abspath(WORKSPACE) == os.path.abspath(os.path.dirname(os.path.dirname(VERIFY))):
    _stage = tempfile.mkdtemp(prefix="verify-selftest-")
    _work = os.path.join(_stage, "repo")
    shutil.copytree(
        os.path.join(WORKSPACE, ".github"), os.path.join(_work, ".github"),
    )
    WORKSPACE = _work
else:
    _stage = None

WF = os.path.join(WORKSPACE, ".github", "workflows", "auto-reading.yml")
DG = os.path.join(WORKSPACE, ".github", "scripts", "weekly_digest.py")
WD = os.path.join(WORKSPACE, ".github", "scripts", "watchdog.py")
LC = os.path.join(WORKSPACE, ".github", "scripts", "last_credit.py")

ORIG = {p: open(p, encoding="utf-8").read() for p in (WF, DG, WD, LC)}

AGE_IF_RE = re.compile(
    r'^[ \t]*if \[ "\$hours" -ge "\$MIN_HOURS" \]; then[ \t]*\n', re.M
)
GATE_EQ = "outputs.should_run == 'true'"
GATE_NE = "outputs.should_run != 'true'"
HELPER_CALL = 'last=$(python .github/scripts/last_credit.py 2>/dev/null) || last=""'
SCHEDULE_ONLY = 'r.get("event") == "schedule"'          # last_credit.py
WD_BOTH_EVENTS = 'r.get("event") in ("schedule", "workflow_dispatch")'   # watchdog
DELEGATED = "    return last_credit.credited_seconds(run_id)"
PER_SOURCE = "inspect_runs = sorted(sched + manual"
BOTH_DAYS = "for days_back in (1, 0):"
DIGEST_SPLIT = "runs = sched_runs + manual_runs"

results = []


def restore():
    for path, text in ORIG.items():
        open(path, "w", encoding="utf-8", newline="").write(text)


def write(path, text):
    open(path, "w", encoding="utf-8", newline="").write(text)


def run(label, expect_failure):
    proc = subprocess.run(
        [sys.executable, VERIFY],
        capture_output=True,
        text=True,
        env={**os.environ, "WORKSPACE": WORKSPACE, "TARGET_MINUTES": "90"},
    )
    fails = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip().startswith("FAIL")]
    caught = proc.returncode != 0
    ok = caught == expect_failure
    results.append(ok)
    print(f"  {'PASS' if ok else 'BROKEN':<7} {label}")
    print(f"          exit={proc.returncode} failures={len(fails)}")
    for f in fails[:4]:
        print(f"            {f}")
    if proc.stderr.strip():
        print(f"            stderr: {proc.stderr.strip()[:200]}")
    return ok


def case_needs(text, anchor, label):
    """An anchor that is missing means the suite itself drifted; fail loudly
    rather than reporting a false PASS because the injection never applied."""
    count = text.count(anchor)
    if count != 1:
        results.append(False)
        print(f"  BROKEN  {label}: anchor appears {count} time(s), expected 1")
        return False
    return True


print(f"workspace: {WORKSPACE}")
print(f"verifier : {VERIFY}\n")

print("case A: the fixed state must pass")
restore()
run("A. current state", expect_failure=False)

print("\ncase B: re-introduce the inverted gate (the bug that cost a day)")
restore()
if case_needs(ORIG[WF], GATE_EQ, "B. gate inverted"):
    write(WF, ORIG[WF].replace(GATE_EQ, GATE_NE))
    run("B. gate inverted", expect_failure=True)

print("\ncase C: delete the age-test if line (the bug I introduced by accident)")
restore()
_hits = AGE_IF_RE.findall(ORIG[WF])
if len(_hits) != 1:
    results.append(False)
    print(f"  BROKEN  C. age-test if removed: regex matched {len(_hits)} line(s), expected 1")
else:
    write(WF, AGE_IF_RE.sub("", ORIG[WF], count=1))
    run("C. age-test if removed", expect_failure=True)

print("\ncase D: digest slot list drifts away from the cron hours")
restore()
if case_needs(ORIG[DG], "(2, 8, 20)", "D. digest slots drifted"):
    write(DG, ORIG[DG].replace("(2, 8, 20)", "(6, 14, 22)"))
    run("D. digest slots drifted", expect_failure=True)

print("\ncase E: drop the timeout headroom check by shrinking it")
restore()
if case_needs(ORIG[WF], "timeout-minutes: 150", "E. timeout too small"):
    write(WF, ORIG[WF].replace("timeout-minutes: 150", "timeout-minutes: 60"))
    run("E. timeout too small for the target", expect_failure=True)

print("\ncase F: revert the guard to the conclusion signal that lied in production")
restore()
if case_needs(ORIG[WF], HELPER_CALL, "F. guard trusts run conclusion"):
    write(WF, ORIG[WF].replace(
        HELPER_CALL,
        'last=$(gh api "repos/$REPO/actions/workflows/$WORKFLOW/runs?event=schedule'
        '&per_page=100" --jq \'[.workflow_runs[] | select(.conclusion=="success")'
        ' | .created_at] | max\')',
    ))
    run("F. guard trusts run conclusion", expect_failure=True)

print("\ncase G: a comment mentioning the helper must not satisfy the wiring check")
restore()
if case_needs(ORIG[WF], HELPER_CALL, "G. helper call removed"):
    write(WF, ORIG[WF].replace(HELPER_CALL, 'last="2026-01-01T00:00:00Z"'))
    run("G. helper call removed", expect_failure=True)

print("\ncase H: let manual dispatches consume the guard's daily budget")
restore()
if case_needs(ORIG[LC], SCHEDULE_ONLY, "H. helper counts manual dispatches"):
    write(LC, ORIG[LC].replace(
        SCHEDULE_ONLY, 'r.get("event") in ("schedule", "workflow_dispatch")'
    ))
    run("H. helper counts manual dispatches", expect_failure=True)

print("\ncase I: let the watchdog grow its own private copy of the measurement")
restore()
if case_needs(ORIG[WD], DELEGATED, "I. watchdog re-implements the read"):
    write(WD, ORIG[WD].replace(
        DELEGATED,
        "    import glob\n"
        "    workdir = tempfile.mkdtemp(prefix=f'wd-run-{run_id}-')\n"
        '    gh(["run", "download", str(run_id), "-R", REPO, "-D", workdir])\n'
        "    total = 0\n"
        "    for path in glob.glob(os.path.join(workdir, '**', 'run-history.json'),"
        " recursive=True):\n"
        '        with open(path, "r", encoding="utf-8") as handle:\n'
        "            total += sum(int(r.get('total_duration_seconds') or 0)"
        " for r in json.load(handle))\n"
        "    return total",
    ))
    run("I. watchdog re-implements the read", expect_failure=True)

print("\ncase J: narrow the watchdog back to scheduled runs only")
restore()
if case_needs(ORIG[WD], WD_BOTH_EVENTS, "J. watchdog ignores manual runs"):
    write(WD, ORIG[WD].replace(WD_BOTH_EVENTS, 'r.get("event") == "schedule"'))
    run("J. watchdog ignores manual runs", expect_failure=True)

print("\ncase K: single quotes must not smuggle conclusion==success past the check")
restore()
CLASSIFY = '        if conclusion != "success":\n            continue\n'
if case_needs(ORIG[DG], CLASSIFY, "K. digest trusts run conclusion"):
    write(DG, ORIG[DG].replace(
        CLASSIFY,
        CLASSIFY + "        if conclusion == 'success':\n            phantom_ok += 1\n",
    ))
    run("K. digest trusts run conclusion", expect_failure=True)

print("\ncase L: removing the cross-midnight span while keeping the identifier")
restore()
if case_needs(ORIG[DG], BOTH_DAYS, "L. digest mis-measures 00:00-02:00 UTC"):
    write(DG, ORIG[DG].replace(BOTH_DAYS, "for days_back in (0,):"))
    run("L. digest mis-measures the 00:00-02:00 UTC window", expect_failure=True)

print("\ncase M: let manual runs displace the scheduled run that actually read")
restore()
if case_needs(ORIG[WD], PER_SOURCE, "M. watchdog shares one inspection cap"):
    write(WD, ORIG[WD].replace(
        PER_SOURCE, "inspect_runs = runs[:MAX_RUNS_TO_INSPECT]"
    ))
    run("M. watchdog shares one inspection cap", expect_failure=True)

print("\ncase N: let the digest bucket days from scheduled runs only")
restore()
if case_needs(ORIG[DG], DIGEST_SPLIT, "N. digest drops manual days"):
    write(DG, ORIG[DG].replace(DIGEST_SPLIT, "runs = sched_runs"))
    run("N. digest drops manual-only days", expect_failure=True)

restore()
passed = sum(1 for r in results if r)
print(f"\n{passed}/{len(results)} verifier cases behaved correctly")

if _stage:
    shutil.rmtree(_stage, ignore_errors=True)

sys.exit(0 if all(results) else 1)
