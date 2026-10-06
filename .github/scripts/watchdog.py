"""Hourly watchdog for the WeRead auto-reading job.

The bug this exists to catch: the reading workflow once reported `success`
while its bot step was silently skipped by an inverted gate condition. Nothing
alerted, and the gap went unnoticed for a day. GitHub's own failure email is
useless there, because the run genuinely "succeeded".

So this checks the OUTCOME, not the run status: has any run in the last
CHECK_WINDOW_HOURS actually credited reading time? If not, it emails.

Both scheduled runs and manual dispatches count toward that answer, because a
90-minute manual session really did bank the reading time -- reporting it as a
failure would be false. The guard's daily budget is deliberately a separate
question: it ignores manual runs so a smoke test cannot suppress the real slot.

Deliberately independent of the reading workflow -- separate file, separate
schedule, separate job -- so a bug in that workflow cannot disable it.
"""

import datetime
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import last_credit  # noqa: E402
import send_mail  # noqa: E402

REPO = os.environ.get("REPO", "")
WORKFLOW = os.environ.get("WORKFLOW", "auto-reading.yml")
CHECK_WINDOW_HOURS = int(os.environ.get("CHECK_WINDOW_HOURS", "26"))
# A 90 minute target; anything under 50 minutes means something is wrong even
# if the run claims success.
MIN_READING_SECONDS = int(os.environ.get("MIN_READING_SECONDS", "3000"))
MAX_RUNS_TO_INSPECT = int(os.environ.get("MAX_RUNS_TO_INSPECT", "6"))
# Manual dispatches get their own budget. Sharing one cap let a handful of manual
# test runs push the scheduled run that actually banked reading time out of the
# inspected slice -- the slice takes the newest N, and manual runs are the newest
# when you are actively testing. That turns a healthy day into a false alarm.
MAX_MANUAL_TO_INSPECT = int(os.environ.get("MAX_MANUAL_TO_INSPECT", "3"))
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

    Delegates to last_credit so the guard and the watchdog can never disagree
    about what counts as credited reading time. A guard-skip legitimately
    records nothing, so 0 is not itself an error -- it just means this run is
    not the one that produced the day's reading.
    """
    return last_credit.credited_seconds(run_id)


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
    # Both sources count toward the daily goal. A 90-minute manual session
    # really did bank the reading time, so calling that window a failure would
    # be a false alarm. The guard's budget is a separate question and still
    # ignores manual runs on purpose -- see the note in the alert body.
    runs = [r for r in runs if r.get("event") in ("schedule", "workflow_dispatch")]
    log(f"{len(runs)} run(s) in the last {CHECK_WINDOW_HOURS}h "
        f"(schedule + manual)")

    # Cap per source, then merge newest-first. A single shared cap meant manual
    # dispatches could displace the scheduled run that actually read, because
    # the cap takes the newest N across both sources.
    sched = [r for r in runs if r["event"] == "schedule"][:MAX_RUNS_TO_INSPECT]
    manual = [r for r in runs if r["event"] == "workflow_dispatch"][:MAX_MANUAL_TO_INSPECT]
    inspect_runs = sorted(sched + manual, key=lambda r: r["created_at"], reverse=True)
    if len(inspect_runs) < len(runs):
        log(f"  inspecting {len(inspect_runs)} of {len(runs)} runs "
            f"({len(sched)} scheduled / {len(manual)} manual)")

    counts = {"schedule": 0, "workflow_dispatch": 0}
    by_source = {"schedule": 0, "workflow_dispatch": 0}
    best = 0
    best_run = None
    best_event = None
    latest = None
    for run in inspect_runs:
        event = run["event"]
        if latest is None:
            latest = run
        seconds = reading_seconds(run["id"])
        counts[event] += 1
        by_source[event] = max(by_source[event], seconds)
        log(f"  run {run['id']} event={event} conclusion={run.get('conclusion')} "
            f"credited={seconds}s")
        if seconds > best:
            best, best_run, best_event = seconds, run["id"], event

    return {
        "runs": len(runs),
        "sched_runs": counts["schedule"],
        "manual_runs": counts["workflow_dispatch"],
        "sched_seconds": by_source["schedule"],
        "manual_seconds": by_source["workflow_dispatch"],
        "best_seconds": best,
        "best_run": best_run,
        "best_event": best_event,
        "latest_id": latest["id"] if latest else None,
        "latest_conclusion": latest.get("conclusion") if latest else None,
    }


def build_body(state, healthy):
    lines = [
        "微信读书自动阅读 —— 独立看门狗告警" if not healthy else "微信读书自动阅读看门狗自检",
        "",
        f"检查窗口：最近 {CHECK_WINDOW_HOURS} 小时",
        f"窗口内运行次数：{state['runs']}"
        f"（定时 {state['sched_runs']} / 手动 {state['manual_runs']}）",
        f"窗口内最长单次计入时长：{state['best_seconds']} 秒"
        f"（阈值 {MIN_READING_SECONDS} 秒）",
        f"  · 定时运行最长 {state['sched_seconds']} 秒"
        f"（达标：{'是' if state['sched_seconds'] >= MIN_READING_SECONDS else '否'}）",
        f"  · 手动运行最长 {state['manual_seconds']} 秒"
        f"（达标：{'是' if state['manual_seconds'] >= MIN_READING_SECONDS else '否'}）",
    ]
    if state["latest_id"]:
        lines.append(f"最近一次运行：{state['latest_id']}（{state['latest_conclusion']}）")
    if state["best_run"]:
        source = "手动触发" if state["best_event"] == "workflow_dispatch" else "定时触发"
        lines.append(f"计入时长的那次运行：{state['best_run']}（{source}，"
                     f"{state['best_seconds']} 秒）")
    lines.append("")

    if healthy:
        lines += [
            f"结论：正常，无需处理。达标来源："
            f"{'手动触发' if state['best_event'] == 'workflow_dispatch' else '定时触发'}。",
            "",
            "这封邮件是你手动触发自检时收到的，不代表出问题了。",
            "",
            "说明：手动运行同样计入阅读时长，但它不消耗守卫的每日额度——",
            "守卫只看定时运行记录的时长，避免一次手动测试就把当天的正式时段挤掉。",
        ]
        return "\n".join(lines)

    lines += ["结论：【异常】窗口内没有任何一次运行真正计入达标时长。", ""]

    if state["manual_seconds"] > 0:
        lines += [
            f"注意：窗口内手动运行计入了 {state['manual_seconds']} 秒，但没有达到 "
            f"{MIN_READING_SECONDS} 秒阈值。",
            "这说明手动触发本身跑起来了、但没读满，问题多半出在阅读过程而不是调度，",
            "所以下面第 1、4 条（调度相关）可以先跳过。",
            "",
        ]
    else:
        lines += ["可能原因（按常见度排序）：", ""]

    lines += [
        "  1. 登录凭据过期，脚本每次都在校验阶段退出（最常见）",
        "  2. 微信读书接口或签名规则变了",
        "  3. 判断步骤把该跑的那些跳过了（gate 极性反了 / MIN_HOURS 过大）",
        "  4. Actions 调度器长时间没有触发",
        "",
        "注意：第 3 种情况下 GitHub 不会发失败邮件——那些运行的 status 确实是",
        "success，只是机器人步骤被跳过了。判断依据只能是本封告警。",
        "",
        "排查顺序：",
        f"  · 打开 https://github.com/{REPO}/actions/workflows/{WORKFLOW}",
        "    找窗口内 credited 大于 0 的那次运行；",
    ]
    if state["manual_seconds"] > 0:
        lines += [
            "  · 手动那次没读满：直接看「Run WeRead Bot」步骤的日志尾部，",
            "    确认它是在 reading 中途停下（网络/接口限流），还是一开始就没进入阅读；",
        ]
    else:
        lines += [
            "  · 先看最近一次运行的「Decide whether this slot should read」步骤，",
            "    打印的是 running this slot 还是 skipping；",
            "  · 若是 skipping，说明判断逻辑误判，去看它打印的 last credited session 时间；",
            "  · 若是 running this slot 但时长仍为 0，看「Run WeRead Bot」",
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