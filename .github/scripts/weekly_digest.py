"""Weekly digest for the WeRead auto-reading bot.

Aggregates the last N days of `auto-reading.yml` runs, sums the reading time the
bot actually recorded, and sends a single email via Apprise. Failure alerts are
handled separately by the reading workflow itself
(NOTIFICATION_ONLY_ON_FAILURE=true), so this job stays at one message a week.

GitHub access goes through the `gh` CLI (preinstalled on runners) because the
artifact download endpoint 302-redirects to a signed URL that rejects a
carried-over Authorization header.
"""

import datetime
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import send_mail  # noqa: E402

REPO = os.environ.get("REPO", "")
WORKFLOW = os.environ.get("WORKFLOW", "auto-reading.yml")
DAYS = int(os.environ.get("DAYS", "7"))
SEND_TEST = os.environ.get("SEND_TEST", "").strip().lower() in ("1", "true", "yes")

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


def history_seconds(run_id):
    """Sum total_duration_seconds from the run's uploaded history artifact."""
    workdir = tempfile.mkdtemp(prefix=f"weread-run-{run_id}-")
    try:
        gh(["run", "download", str(run_id), "-R", REPO, "-D", workdir])
    except Exception as exc:  # noqa: BLE001 - one bad run must not kill the digest
        log(f"run {run_id}: artifact download failed: {exc}")
        return 0

    total = 0
    try:
        for path in glob.glob(os.path.join(workdir, "**", "run-history.json"), recursive=True):
            with open(path, "r", encoding="utf-8") as handle:
                records = json.load(handle)
            total += sum(int(r.get("total_duration_seconds") or 0) for r in records)
    except Exception as exc:  # noqa: BLE001
        log(f"run {run_id}: could not parse run-history.json: {exc}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return total


def humanize(seconds):
    seconds = int(seconds or 0)
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    if hours and minutes:
        return f"{hours} 小时 {minutes} 分钟"
    if hours:
        return f"{hours} 小时"
    return f"{minutes} 分钟"


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

    # only count the nightly schedule, not manual smoke tests
    runs = [r for r in runs if r.get("event") == "schedule"]

    total_seconds = 0
    ok = 0
    failed_rows = []
    for run in runs:
        conclusion = run.get("conclusion")
        if conclusion == "success":
            ok += 1
            total_seconds += history_seconds(run["id"])
        elif conclusion in FAILURE_CONCLUSIONS:
            failed_rows.append(
                (run.get("created_at", "")[:16].replace("T", " "), conclusion)
            )

    end = datetime.datetime.now().astimezone()
    start = (end - datetime.timedelta(days=DAYS)).astimezone()

    lines = [
        f"微信读书周报（{start:%Y-%m-%d} ~ {end:%Y-%m-%d}）",
        "",
        f"定时任务执行：{len(runs)} 次",
        f"成功：{ok}    失败：{len(failed_rows)}",
        f"累计阅读时长：{humanize(total_seconds)}",
        "",
        f"仓库：{REPO}",
        "计划：每天 22:00 跑一次，时长 55-65 分钟",
    ]
    if failed_rows:
        lines += ["", "失败记录："]
        lines += [f"  - {when} UTC  {conclusion}" for when, conclusion in failed_rows]
        lines += ["", "失败详情见 Actions 页面该次运行记录。"]
    return "\n".join(lines), len(runs), ok, len(failed_rows), total_seconds


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
        report, total, ok, failed, seconds = build_report()
    except Exception as exc:  # noqa: BLE001
        log(f"could not build the report: {type(exc).__name__}: {exc}")
        return 1

    print("----- digest preview -----")
    print(report)
    print("--------------------------")
    log(f"runs={total} ok={ok} failed={failed} seconds={seconds}")

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
