"""Invariant checks for the GitHub Actions wiring.

Written after a gate condition was left inverted: the reading job reported
`success` for a day while never executing, because renaming an output from
`already_ok` (skip == true) to `should_run` (go == true) did not update the
consumer. A boolean gate is a contract between two steps, so the contract is
asserted here instead of trusted.

Run on every push. Fails loudly rather than letting a semantic flip through.
"""

import os
import re
import subprocess
import sys

import yaml

ROOT = os.environ.get("WORKSPACE", ".")
READING = os.path.join(ROOT, ".github/workflows/auto-reading.yml")
DIGEST = os.path.join(ROOT, ".github/workflows/weekly-digest.yml")
DIGEST_PY = os.path.join(ROOT, ".github/scripts/weekly_digest.py")
GUARD_PY = os.path.join(ROOT, ".github/scripts/watchdog.py")
TARGET_MINUTES = int(os.environ.get("TARGET_MINUTES", "90"))

failures = []
checks = 0


def check(ok, label, detail=""):
    global checks
    checks += 1
    if ok:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}" + (f"\n          {detail}" if detail else ""))
        failures.append(label)


def steps_of(doc, job):
    return {s.get("name"): s for s in doc["jobs"][job]["steps"] if s.get("name")}


def bash_syntax_ok(script):
    """`bash -n` without executing. Catches an if/else/fi left unbalanced.

    Returns (True, ""), (False, error) or (None, reason) when the local bash
    cannot be trusted -- some Windows shells pipe stdin badly enough that even
    valid input is rejected. Skipping beats failing CI over a broken shell.
    """
    probe = "if [ 1 -eq 1 ]; then\n  echo ok\nfi\n"
    try:
        sanity = subprocess.run(
            ["bash", "-n"], input=probe, capture_output=True, text=True, timeout=30
        )
        if sanity.returncode != 0:
            return None, f"local bash rejects a known-good script ({sanity.returncode})"
        proc = subprocess.run(
            ["bash", "-n"], input=script, capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"could not run bash: {exc}"
    if proc.returncode == 0:
        return True, ""
    return False, proc.stderr.strip()[:400]


def main():
    with open(READING, encoding="utf-8") as handle:
        reading = yaml.safe_load(handle)
    with open(DIGEST, encoding="utf-8") as handle:
        digest = yaml.safe_load(handle)

    triggers = reading.get(True) or reading["on"]
    rsteps = steps_of(reading, "auto-reading")

    print("\n1. the bot step must exist and be gated")
    bot = rsteps.get("Run WeRead Bot")
    guard = rsteps.get("Decide whether this slot should read")
    check(bot is not None, "Run WeRead Bot step exists")
    check(guard is not None, "guard step exists")
    if not (bot and guard):
        return finish()

    expr = bot.get("if", "")

    # --- the contract itself ---
    print("\n2. gate polarity, evaluated rather than pattern-matched")
    parsed = re.search(
        r"github\.event_name\s*!=\s*'schedule'\s*\|\|\s*"
        r"steps\.guard\.outputs\.(\w+)\s*(==|!=)\s*'([^']*)'",
        expr,
    )
    check(bool(parsed), "gate expression has the expected shape", f"got: {expr!r}")
    if not parsed:
        return finish()

    name, op, value = parsed.group(1), parsed.group(2), parsed.group(3)

    def gate(event, output_value):
        first = event != "schedule"
        second = (output_value == value) if op == "==" else (output_value != value)
        return first or second

    expected = {
        ("workflow_dispatch", "true"): True,
        ("workflow_dispatch", "false"): True,
        ("schedule", "true"): True,
        ("schedule", "false"): False,
    }
    for (event, out), want in expected.items():
        got = gate(event, out)
        check(
            got == want,
            f"gate({event}, {name}={out}) -> run={got}",
            f"expected run={want}; check the operator ({op} '{value}')"
            + ("  <-- this is the inverted-gate bug"
               if (got != want) else ""),
        )

    # --- the producer must agree with the consumer ---
    print("\n3. the guard emits the output the gate expects")
    workflow_text = open(READING, encoding="utf-8").read()
    guard_run = guard.get("run", "")
    # Wires are checked against code, never against prose. A comment that merely
    # names a file must not be able to satisfy a wiring check: case G in the
    # verifier self-test removes the real invocation and leaves the comment, and
    # an earlier version of this check passed that broken state.
    guard_code = "\n".join(
        ln for ln in guard_run.splitlines() if not ln.lstrip().startswith("#")
    )
    check(
        f'echo "{name}=true"' in guard_run and f'echo "{name}=false"' in guard_run,
        f"guard emits both {name}=true and {name}=false",
    )
    manual_bypass = 'if [ "$EVENT_NAME" != "schedule" ]' in guard_run
    check(manual_bypass, "guard short-circuits for manual dispatch")
    # the branch that means "go" must be the one that satisfies the gate
    go_branch = re.search(
        r"if \[ \"\$hours\" -ge \"\$MIN_HOURS\" \]; then(.*?)\n\s*fi\b", guard_run, re.S
    )
    check(bool(go_branch), "guard has an age test branch")
    if go_branch:
        emits_go = f'echo "{name}=true"' in go_branch.group(1)
        check(emits_go, f"the age-test pass branch emits {name}=true (the value the gate runs on)")

    # --- the guard must at least be valid shell ---
    print("\n3b. the guard script must be syntactically valid shell")
    ok, err = bash_syntax_ok(guard_run)
    if ok is None:
        print(f"  SKIP  bash unusable here ({err})")
    else:
        check(ok, "bash -n accepts the guard script", err)
        # an if/else/fi left unbalanced by a bad edit is the failure this catches
        check(
            guard_run.count("if [") >= 3 and guard_run.count("fi") >= 3,
            "guard if/fi counts balance",
            f"if=[={guard_run.count('if [')} fi]={guard_run.count('fi')}",
        )
    stale = re.findall(r"outputs\.(\w+)\s*[=!]=\s*'true'", workflow_text)
    check(
        set(stale) <= {name},
        "no other gate outputs referenced",
        f"found: {sorted(set(stale))}",
    )

    # --- the guard must not trust a proxy that has already lied once ---
    print("\n3a. the guard must read credited time, not the run conclusion")
    helper = os.path.join(ROOT, ".github/scripts/last_credit.py")
    check(os.path.exists(helper), "last_credit.py present")
    check(
        "python .github/scripts/last_credit.py" in guard_code,
        "guard delegates the credit lookup to last_credit.py",
        "the guard is deciding on its own again",
    )
    check(
        'conclusion=="success"' not in guard_code and "conclusion == 'success'" not in guard_code,
        "guard does not select runs by conclusion",
        "conclusion==success lied during the inverted-gate incident",
    )
    check(
        "if-no-files-found" not in guard_code,
        "guard does not infer anything from artifact presence",
    )
    if os.path.exists(helper):
        with open(helper, encoding="utf-8") as handle:
            helper_text = handle.read()
        # anchor on the read itself; the module docstring names the same field
        check(
            'record.get("total_duration_seconds")' in helper_text,
            "helper reads total_duration_seconds from run-history.json",
        )
        check(
            'event") == "schedule"' in helper_text
            or "event') == 'schedule'" in helper_text,
            "helper ignores manual dispatches so tests cannot consume the budget",
        )
        # the watchdog must reach the same verdict, so it must not re-implement
        # the measurement. Checking merely for the field name was too weak: a
        # private copy of the whole read satisfies it while drifting.
        wd_early = os.path.join(ROOT, ".github/scripts/watchdog.py")
        if os.path.exists(wd_early):
            with open(wd_early, encoding="utf-8") as handle:
                wd_early_text = handle.read()
            wd_code = "\n".join(
                ln for ln in wd_early_text.splitlines() if not ln.lstrip().startswith("#")
            )
            check(
                "import last_credit" in wd_code,
                "watchdog imports the shared credit helper",
            )
            check(
                "last_credit.credited_seconds(" in wd_code,
                "watchdog measures credited seconds through the shared helper",
            )
            check(
                "run-history.json" not in wd_code,
                "watchdog does not re-implement the artifact read",
                "a private copy of this logic is how the two would drift apart",
            )
            # a real 90-minute manual session banks real reading time; a
            # schedule-only filter reported that window as a failure. Anchored on
            # the filter expression itself: the display code in build_body also
            # mentions workflow_dispatch, so a bare substring was satisfiable by
            # presentation alone (self-test case J).
            check(
                'r.get("event") in ("schedule", "workflow_dispatch")' in wd_code,
                "watchdog counts manual dispatches toward the daily goal",
                "a 90-minute manual run must not read as a failed window",
            )
            check(
                "定时运行最长" in wd_early_text and "手动运行最长" in wd_early_text,
                "watchdog reports the two sources separately",
                "otherwise the alert cannot tell the user which path delivered",
            )

    # --- schedule shape ---
    print("\n4. schedule and timeout")
    crons = [s["cron"] for s in triggers["schedule"]]
    check(len(crons) == 3, f"exactly 3 daily slots, found {len(crons)}: {crons}")
    hours = sorted(int(c.split()[1]) for c in crons)
    check(len(set(hours)) == len(hours), "slot hours are distinct", str(hours))
    for c in crons:
        parts = c.split()
        check(len(parts) == 5, f"cron {c!r} has 5 fields")
    timeout = reading["jobs"]["auto-reading"].get("timeout-minutes", 0)
    check(
        timeout >= TARGET_MINUTES + 30,
        f"timeout-minutes {timeout} leaves >=30 min headroom over the {TARGET_MINUTES} min target",
    )

    # --- cross-file coupling that silently drifts ---
    print("\n5. digest slot list must match the workflow cron hours")
    digest_text = open(DIGEST_PY, encoding="utf-8").read()
    match = re.search(r"SLOT_HOURS_UTC\s*=\s*\(([^)]*)\)", digest_text)
    check(bool(match), "SLOT_HOURS_UTC declared in weekly_digest.py")
    if match:
        declared = sorted(int(x) for x in re.findall(r"\d+", match.group(1)))
        check(
            declared == hours,
            f"weekly_digest SLOT_HOURS_UTC {declared} == workflow cron hours {hours}",
            "the delay report would be computed against the wrong slots",
        )
    check("send_mail" in digest_text, "digest uses send_mail (no apprise dependency)")
    apprise_used = re.search(r"^\s*import\s+apprise\b|apprise\.Apprise\(", digest_text, re.M)
    check(
        apprise_used is None,
        "weekly_digest does not actually use apprise",
        "apprise>=2 dropped smtp://, so any use of it silently breaks email",
    )

    # --- the digest must report credited time, not the run conclusion ---
    print("\n5a. the digest must report credited time, not the run conclusion")
    dg_code = "\n".join(
        ln for ln in digest_text.splitlines() if not ln.lstrip().startswith("#")
    )
    check(
        "last_credit.credited_seconds(" in dg_code,
        "digest measures credited time through the shared helper",
        "three copies of this read existed; the guard and watchdog now share one",
    )
    check(
        "history_seconds" not in dg_code,
        "digest does not keep a private copy of the artifact read",
        "a private copy is how the three implementations would drift apart",
    )
    # quote-style agnostic: self-test case K injects single quotes, and a
    # double-quote-only anchor let that pass
    conclusion_success = re.search(r"""conclusion\s*==\s*["']success["']""", dg_code)
    check(
        conclusion_success is None,
        "digest does not count conclusion==success as a success",
        "12 of 18 audited runs reported success while reading nothing at all",
    )
    check(
        "跳过（未计入时长）" in digest_text and "每日达标情况" in digest_text,
        "digest separates skipped runs and prints a per-day table",
        "a day with zero reading must show up as an explicit zero, not vanish",
    )
    check(
        "距上次真正计入阅读时长" in digest_text
        and "距上次成功满 20 小时" not in digest_text,
        "digest describes the 20h rule in terms of credited time",
        "'last success' is the wrong mental model that caused the guard bug",
    )
    # anchored on the both-days expression, not merely the identifier: reducing
    # it to (0,) leaves the name in place and silently reintroduces the ~25h
    # delay (self-test case L)
    check(
        "for days_back in (1, 0):" in dg_code,
        "schedule_delay_minutes spans candidate slots across midnight",
        "a run firing 00:00-02:00 UTC was reported ~25h late instead of ~5h",
    )

    # --- the failure alert must still be wired ---
    print("\n6. alerting")
    check("Notify by email on failure" in rsteps, "failure email step exists")
    check(rsteps["Notify by email on failure"].get("if") == "failure()", "failure step is guarded by if: failure()")
    check("Upload runtime artifacts" in rsteps, "artifact upload exists")

    # --- the watchdog window must be consistent with the cron geometry ---
    print("\n7. watchdog window is derived from the schedule, not guessed")
    wd_wf_path = os.path.join(ROOT, ".github/workflows/watchdog.yml")
    wd_path = os.path.join(ROOT, ".github/scripts/watchdog.py")
    check(os.path.exists(wd_wf_path), "watchdog workflow present")
    check(os.path.exists(wd_path), "watchdog script present")
    if os.path.exists(wd_wf_path):
        with open(wd_wf_path, encoding="utf-8") as handle:
            wd_doc = yaml.safe_load(handle)
        wd_steps = {
            s.get("name"): s for s in wd_doc["jobs"]["watchdog"]["steps"] if s.get("name")
        }
        step = wd_steps.get("Check whether reading time actually accumulated")
        check(step is not None, "watchdog inspection step exists")
        if step:
            raw = str(step.get("env", {}).get("CHECK_WINDOW_HOURS", ""))
            check(raw.isdigit(), f"CHECK_WINDOW_HOURS is a plain number ({raw!r})")
            window = int(raw) if raw.isdigit() else None

            min_hours = int(str(guard.get("env", {}).get("MIN_HOURS", "20")))
            check(min_hours == 20, f"guard MIN_HOURS is 20 (found {min_hours})")

            derive = os.path.join(ROOT, ".github/scripts/derive_window.py")
            check(os.path.exists(derive), "derive_window.py present")
            if window and os.path.exists(derive):
                sys.path.insert(0, os.path.dirname(derive))
                import derive_window  # noqa: E402

                stats = derive_window.analyze(min_hours=min_hours)
                widest = stats["max_h"]
                print(
                    f"        widest normal gap at {stats['jitter_hours']}h jitter "
                    f"= {widest:.1f}h, watchdog window = {window}h"
                )
                check(
                    window > widest,
                    f"watchdog window {window}h exceeds the widest normal gap {widest:.1f}h",
                    "a healthy system would trigger a false alarm",
                )
                check(
                    window <= 30,
                    f"watchdog window {window}h is <= 30h",
                    "a fully silent failure would go unnoticed for too long",
                )

    print("\n8. watchdog exists and is independent")
    check(os.path.exists(GUARD_PY), "watchdog.py present")
    with open(GUARD_PY, encoding="utf-8") as handle:
        wd_text = handle.read()
    check("MIN_READING_SECONDS" in wd_text, "watchdog has a credited-reading threshold")
    check("reading_seconds" in wd_text, "watchdog inspects real credited time, not just run status")

    return finish()


def finish():
    print(f"\n{checks - len(failures)}/{checks} checks passed")
    if failures:
        print("FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("RESULT: all wiring invariants hold")
    return 0


if __name__ == "__main__":
    sys.exit(main())