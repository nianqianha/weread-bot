"""Hourly watchdog for the WeRead auto-reading job.

The bug this exists to catch: the reading workflow once reported `success`
while its bot step was silently skipped by an inverted gate condition. Nothing
alerted, and the gap went unnoticed for a day. GitHub's own failure email is
useless there, because the run genuinely "succeeded".

So this checks the OUTCOME, not the run status: has any scheduled run in the
last CHECK_WINDOW_HOURS actually credited reading time? If not, it emails.

Deliberately independent of the reading workflow -- separate file, separate
schedule, separate job -- so a bug in that workflow cannot disable it.
"""

import datetime
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import send_mail  # noqa: E402

REPO = os.environ.get("REPO", "")
WORKFLOW = os.environ.get("WORKFLOW", "auto-reading.yml")
CHECK_WINDOW_HOURS = int(os.environ.get("CHECK_WINDOW_HOURS", "26"))
# A 90 minute target; anything under 50 minutes means something is wrong even
# if the run claims success.
MIN_READING_SECONDS = int(os.environ.get("MIN_READING_SECONDS", "3000"))
MAX_RUNS_TO_INSPECT = int(os.environ.get("MAX_RUNS_TO_INSPECT", "6"))
TEST_MODE = os.environ.get("TEST_MODE", "").strip().lower() in ("1", "true", "yes")


def log(msg):
    print(f"[watchdog] {msg}", flush=True)


def gh(args, timeout=180):
    env = dict(os.environ)
    env.setdefault("GH_TOKEN", os.environ.get("GITHUB_TOKEN", ""))
    proc = subprocess.run(["gh", *args], capture_output=True, timeout=timeout, env=env)
    if proc.returncode != 0:
        raise RuntimeError(
            f"gh {' '.join(args)} failed: {proc.stderr.decode('utf-8', 'replace').strip()}"
        )
    return proc.stdout


def reading_seconds(run_id):
    """Credited reading seconds recorded by this run, or 0 if it recorded none.

    A guard-skip legitimately records nothing, so 0 is not itself an error --
    it just means this run is not the one that produced the day's reading.
    """
    workdir = tempfile.mkdtemp(prefix=f"wd-run-{run_id}-")
    try:
        gh(["run", "download", str(run_id), "-R", REPO, "-D", workdir])
    except Exception as exc:  # noqa: BLE001
        log(f"run {run_id}: artifact unavailable ({str(exc)[:80]})")
        return 0

    total = 0
    try:
        import glob

        for path in glob.glob(os.path.join(workdir, "**", "run-history.json"), recursive=True):
            with open(path, "r", encoding="utf-8") as handle:
                records = json.load(handle)
            total += sum(int(r.get("total_duration_seconds") or 0) for r in records)
    except Exception as exc:  # noqa: BLE001
        log(f"run {run_id}: could not read run-history.json ({exc})")
    finally:
        import shutil

        shutil.rmtree(workdir, ignore_errors=True)
    return total


def inspect():
    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        hours=CHECK_WINDOW_HOURS
    )
    since_s = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    runs = json.loads(
        gh(
            [
                "api",
                f"repos/{REPO}/actions/workflows/{WORKFLOW}/runs"
                f"?created=>={since_s}&per_page=30",
            ]
        ).decode("utf-8")
    ).get("workflow_runs", [])
    runs = [r for r in runs if r.get("event") == "schedule"]
    log(f"{len(runs)} scheduled run(s) in the last {CHECK_WINDOW_HOURS}h")

    best = 0
    best_run = None
    for run in runs[:MAX_RUNS_TO_INSPECT]:
        seconds = reading_seconds(run["id"])
        log(f"  run {run['id']} conclusion={run.get('conclusion')} credited={seconds}s")
        if seconds > best:
            best, best_run = seconds, run["id"]

    latest = runs[0] if runs else None
    return {
        "runs": len(runs),
        "best_seconds": best,
        "best_run": best_run,
        "latest_id": latest["id"] if latest else None,
        "latest_conclusion": latest.get("conclusion") if latest else None,
    }


def build_body(state, healthy):
    lines = [
        "微信读书自动阅读 —— 独立看门狗告警" if not healthy else "微信读书自动阅读看门狗自检",
        "",
        f"检查窗口：最近 {CHECK_WINDOW_HOURS} 小时",
        f"窗口内定时运行次数：{state['runs']}",
        f"窗口内最长单次计入时长：{state['best_seconds']} 秒"
        f"（阈值 {MIN_READING_SECONDS} 秒）",
    ]
    if state["latest_id"]:
        lines.append(f"最近一次运行：{state['latest_id']}（{state['latest_conclusion']}）")
    lines.append("")

    if healthy:
        lines += [
            "结论：正常，无需处理。",
            "",
            "这封邮件是你手动触发自检时收到的，不代表出问题了。",
        ]
    else:
        lines += [
            "结论：【异常】窗口内没有任何一次运行真正计入阅读时长。",
            "",
            "可能原因（按常见度排序）：",
            "  1. 判断步骤把该跑的那些跳过了（gate 极性反了 / MIN_HOURS 过大）",
            "  2. 登录凭据过期，脚本每次都在校验阶段退出",
            "  3. 微信读书接口或签名规则变了",
            "  4. Actions 调度器长时间没有触发",
            "",
            "注意：这种情况下 GitHub 不会发失败邮件——那些运行的 status 确实是",
            "success，只是机器人步骤被跳过了。判断依据只能是本封告警。",
            "",
            "排查顺序：",
            f"  · 打开 https://github.com/{REPO}/actions/workflows/{WORKFLOW}",
            "    看最近一次运行的步骤 8「Decide whether this slot should read」",
            "    打印的是 running this slot 还是 skipping；",
            "  · 若是 skipping，说明判断逻辑误判；",
            "  · 若是 running this slot 但时长仍为 0，看步骤 9「Run WeRead Bot」",
            "    的日志，通常是 curl 校验失败或 Cookie 刷新失败。",
        ]
    return "\n".join(lines)


def main():
    if not REPO:
        log("REPO is not set")
        return 1

    try:
        state = inspect()
    except Exception as exc:  # noqa: BLE001
        log(f"inspection failed: {type(exc).__name__}: {exc}")
        log("cannot verify, staying silent rather than crying wolf")
        return 0

    healthy = state["best_seconds"] >= MIN_READING_SECONDS
    log(f"best={state['best_seconds']}s threshold={MIN_READING_SECONDS}s healthy={healthy}")

    body = build_body(state, healthy)

    if TEST_MODE:
        print("----- watchdog preview -----")
        print(body)
        print("-----------------------------")
        return 0

    if healthy:
        log("healthy, no email")
        return 0

    if not os.environ.get("MAIL_CONFIG", "").strip():
        log("MAIL_CONFIG not set, cannot alert")
        return 0

    code = send_mail.send(
        send_mail.load_config(os.environ["MAIL_CONFIG"]),
        "微信读书看门狗告警：窗口内无阅读时长",
        body,
    )
    log("alert sent" if code == 0 else f"alert failed (rc={code})")
    return code


if __name__ == "__main__":
    sys.exit(main())