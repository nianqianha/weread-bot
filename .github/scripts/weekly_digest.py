"""Weekly digest for the WeRead auto-reading bot.

Aggregates the last N days of `auto-reading.yml` runs, sums the reading time the
bot actually recorded, and sends a single email. Failure alerts are handled
separately by the reading workflow itself
(NOTIFICATION_ONLY_ON_FAILURE=true), so this job stays at one message a week.

What counts as a success here is credited reading time, never the run
conclusion. A run whose bot step was skipped by the guard still reports
conclusion=success, so counting those produced a digest that said "18
successful runs" while 12 of them read nothing. Credited time is measured
through last_credit.credited_seconds, the same helper the reading guard and the
watchdog use, so the three can never disagree about what happened.

GitHub access goes through the `gh` CLI (preinstalled on runners) because the
artifact download endpoint 302-redirects to a signed URL that rejects a
carried-over Authorization header. Note that `gh run download` exits 0 even
when it matched no artifact, so return codes cannot be used to detect that.
"""

import datetime
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import last_credit  # noqa: E402
import send_mail  # noqa: E402

REPO = os.environ.get("REPO", "")
WORKFLOW = os.environ.get("WORKFLOW", "auto-reading.yml")
DAYS = int(os.environ.get("DAYS", "7"))
SEND_TEST = os.environ.get("SEND_TEST", "").strip().lower() in ("1", "true", "yes")

# UTC hours of the scheduled slots declared in auto-reading.yml. Those cron
# values are the desired Beijing fire times minus the measured GitHub scheduler
# delay (~5h47m), so they land on roughly 10:00 / 16:00 / 22:00 Beijing.
# Used to report how late each run actually fired.
SLOT_HOURS_UTC = (2, 8, 20)

# The daily reading goal. Kept as an env knob so the digest and the schedule
# cannot drift into arguing about two different targets.
TARGET_MINUTES = int(os.environ.get("TARGET_MINUTES", "90"))
TARGET_SECONDS = TARGET_MINUTES * 60

FAILURE_CONCLUSIONS = {
    "failure",
    "cancelled",
    "timed_out",
    "startup_failure",
    "action_required",
}


def log(msg):
    print(f"[digest] {msg}", flush=True)


def gh(args, timeout=180):
    env = dict(os.environ)
    env.setdefault("GH_TOKEN", os.environ.get("GITHUB_TOKEN", ""))
    proc = subprocess.run(
        ["gh", *args], capture_output=True, timeout=timeout, env=env
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"gh {' '.join(args)} failed: {proc.stderr.decode('utf-8', 'replace').strip()}"
        )
    return proc.stdout


def humanize(seconds):
    seconds = int(seconds or 0)
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    if hours and minutes:
        return f"{hours} 小时 {minutes} 分钟"
    if hours:
        return f"{hours} 小时"
    return f"{minutes} 分钟"


BEIJING = datetime.timezone(datetime.timedelta(hours=8))


def schedule_delay_minutes(created_at):
    """Minutes between the slot this run belongs to and when it actually fired.

    The candidate set must span the previous day as well. A run belonging to the
    20:00 UTC slot can fire after 00:00 UTC the next day -- and the measured
    scheduler delay puts it squarely in that window. Anchoring the candidates at
    the same day's midnight left the set empty for those runs, so the fallback
    was midnight - 1 day and a real ~5h delay was reported as ~25h.
    """
    fired = datetime.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    midnight = fired.replace(hour=0, minute=0, second=0, microsecond=0)
    candidates = []
    for days_back in (1, 0):
        base = midnight - datetime.timedelta(days=days_back)
        candidates += [base.replace(hour=h) for h in SLOT_HOURS_UTC]
    due = [c for c in candidates if c <= fired]
    slot = max(due) if due else midnight - datetime.timedelta(days=1)
    return int(round((fired - slot).total_seconds() / 60))


def timing_lines(rows):
    """rows: (beijing_dt, delay_minutes, credited_seconds) for runs that read.

    Only runs that actually banked time are listed. A guard-skip fires on the
    same schedule but contributes nothing, so mixing it in made the average
    describe "when did the workflow start" instead of "when did reading happen".
    """
    if not rows:
        return ["", "触发时间：本周没有任何一次运行真正计入时长。"]
    rows = sorted(rows)
    out = ["", "触发时间（北京时间 / 相对计划延迟，仅统计真正计入时长的运行）："]
    for when, delay, secs in rows:
        out.append(f"  {when:%m-%d %H:%M}  +{delay} 分钟   计入 {humanize(secs)}")
    delays = [d for _, d, _ in rows]
    avg = int(round(sum(delays) / len(delays)))
    worst = max(delays)
    out.append(f"平均延迟 {avg} 分钟，最大 {worst} 分钟")
    if any(d >= 12 * 60 for d in delays):
        out.append("注意：有运行延迟超过 12 小时，可能跨过午夜导致时长记到次日。")
    return out


def day_lines(day_runs, day_totals):
    """One row per Beijing date that had at least one scheduled run.

    Days that had runs but banked nothing must show up as an explicit zero. A
    day that silently drops out of the table is precisely the signal the reader
    needs -- that is how 2026-10-01, an entire day with no reading at all, went
    unreported inside a digest that said "18 successful runs".
    """
    out = ["", f"每日达标情况（目标 {TARGET_MINUTES} 分钟）："]
    hit = 0
    for day in sorted(day_runs):
        secs = day_totals.get(day, 0)
        if secs >= TARGET_SECONDS:
            hit += 1
            mark = "✓"
        else:
            mark = "✗"
        detail = humanize(secs)
        if secs < TARGET_SECONDS:
            detail += f"（欠 {humanize(TARGET_SECONDS - secs)}）"
        out.append(f"  {mark} {day}  {detail}")
    out.append(f"达标 {hit} / {len(day_runs)} 天")
    return out


def build_report():
    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=DAYS)
    since_s = since.strftime("%Y-%m-%dT%H:%M:%SZ")

    raw = gh(
        [
            "api",
            f"repos/{REPO}/actions/workflows/{WORKFLOW}/runs?created=>={since_s}&per_page=100",
        ]
    )
    runs = json.loads(raw.decode("utf-8")).get("workflow_runs", [])

    # only count the scheduled slots, not manual smoke tests
    runs = [r for r in runs if r.get("event") == "schedule"]

    total_seconds = 0
    read_runs = []          # (beijing_dt, delay_minutes, credited_seconds)
    skipped = []            # succeeded but banked nothing -- the guard let it go
    failed_rows = []
    day_runs = {}           # Beijing date -> how many scheduled runs it had
    day_totals = {}         # Beijing date -> credited seconds

    for run in runs:
        conclusion = run.get("conclusion")
        created = run.get("created_at", "")
        if conclusion in FAILURE_CONCLUSIONS:
            failed_rows.append((created[:16].replace("T", " "), conclusion))
            continue
        if conclusion != "success":
            continue

        # Credited time is the only trustworthy signal. `conclusion == success`
        # is not: a run whose bot step was skipped still reports success, and
        # this digest used to count those as successes -- 12 of 18 runs in the
        # audited week read nothing at all while the email said "18 successful".
        seconds = last_credit.credited_seconds(run["id"])
        log(f"run {run['id']} credited={seconds}s")

        fired = None
        if created:
            fired = datetime.datetime.fromisoformat(
                created.replace("Z", "+00:00")
            ).astimezone(BEIJING)
            day_runs[fired.date()] = day_runs.get(fired.date(), 0) + 1

        if seconds <= 0:
            skipped.append(run["id"])
            continue

        total_seconds += seconds
        if fired is not None:
            day_totals[fired.date()] = day_totals.get(fired.date(), 0) + seconds
            read_runs.append((fired, schedule_delay_minutes(created), seconds))

    end = datetime.datetime.now().astimezone()
    start = (end - datetime.timedelta(days=DAYS)).astimezone()

    target_total = TARGET_SECONDS * DAYS
    if total_seconds >= target_total:
        gap_line = "缺口：无，已超出目标"
    else:
        gap_line = f"缺口：{humanize(target_total - total_seconds)}"

    lines = [
        f"微信读书周报（{start:%Y-%m-%d} ~ {end:%Y-%m-%d}）",
        "",
        f"定时任务执行：{len(runs)} 次",
        f"  真正计入时长：{len(read_runs)} 次",
        f"  跳过（未计入时长）：{len(skipped)} 次",
        f"  运行失败：{len(failed_rows)} 次",
        "",
        f"累计阅读时长：{humanize(total_seconds)}",
        f"目标：每天 {TARGET_MINUTES} 分钟 × {DAYS} 天 = {humanize(target_total)}",
        gap_line,
        "",
        "「跳过」是守卫的正常行为，不是故障：当日已达标时，后续时段会主动让位，",
        "所以同一天通常只有一次运行真正计入时长。需要关注的是「运行失败」",
        "以及连续多日未达标。",
    ]
    lines += day_lines(day_runs, day_totals)
    lines += [
        "",
        f"仓库：{REPO}",
        "计划：每天 3 个时间点（10:00 / 16:00 / 22:00 北京时间），",
        f"      距上次真正计入阅读时长满 20 小时才读，故每天约一次，目标 {TARGET_MINUTES} 分钟。",
    ]
    lines += timing_lines(read_runs)
    if failed_rows:
        lines += ["", "失败记录："]
        lines += [f"  - {when} UTC  {conclusion}" for when, conclusion in failed_rows]
        lines += ["", "失败详情见 Actions 页面该次运行记录。"]
    return (
        "\n".join(lines),
        len(runs),
        len(read_runs),
        len(skipped),
        len(failed_rows),
        total_seconds,
    )


def send_email(subject, body):
    """Hand the message to send_mail.py, which owns the SMTP conversation."""
    body_file = os.path.join(tempfile.gettempdir(), "weread-digest-body.txt")
    with open(body_file, "w", encoding="utf-8") as handle:
        handle.write(body)

    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "send_mail.py")
    env = dict(os.environ)
    env.setdefault("MAIL_CONFIG", "")
    proc = subprocess.run(
        [sys.executable, script, subject, body_file],
        capture_output=True,
        timeout=180,
        env=env,
    )
    out = proc.stdout.decode("utf-8", "replace").strip()
    err = proc.stderr.decode("utf-8", "replace").strip()
    for line in (out or err).splitlines():
        log(line)
    try:
        os.remove(body_file)
    except OSError:
        pass
    return proc.returncode


def main():
    if not REPO:
        log("REPO is not set")
        return 1

    if SEND_TEST:
        log("send_test requested, sending a test email instead of the digest")
        code = send_email(
            "微信读书周报 - 测试邮件",
            "这是一封测试邮件，用于确认 SMTP 配置是否正确。\n\n"
            f"仓库：{REPO}\n"
            f"发送时间：{datetime.datetime.now().astimezone():%Y-%m-%d %H:%M:%S %Z}\n\n"
            "收到这封邮件说明每周汇总可以正常投递。",
        )
        log("test email sent" if code == 0 else "test email failed")
        return code

    try:
        report, total, read_count, skipped, failed, seconds = build_report()
    except Exception as exc:  # noqa: BLE001
        log(f"could not build the report: {type(exc).__name__}: {exc}")
        return 1

    print("----- digest preview -----")
    print(report)
    print("--------------------------")
    log(f"runs={total} read={read_count} skipped={skipped} failed={failed} seconds={seconds}")

    # Only skip when there was nothing to report at all. A window with runs but
    # zero credited time is exactly the case the reader needs told about, so it
    # must still send.
    if total == 0:
        log("no scheduled runs in the window, skipping email")
        return 0

    if not os.environ.get("MAIL_CONFIG", "").strip():
        log("MAIL_CONFIG not set, skipping email (set the secret to enable)")
        return 0

    code = send_email("微信读书周报", report)
    log("weekly digest email sent" if code == 0 else "weekly digest email failed")
    return code


if __name__ == "__main__":
    sys.exit(main())
