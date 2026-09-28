"""Send one email over SMTP using only the standard library.

Deliberately does not use apprise: apprise 2.x dropped the generic `smtp://`
plugin, and weread-bot pins `apprise>=1.9.0`, so any smtp:// URL would fail
silently on the runner.

Configuration comes from the MAIL_CONFIG repository secret, a single-line JSON
object, for example:
  {"host":"smtp.qq.com","port":465,"user":"me@qq.com","password":"AUTHCODE",
   "to":"me@qq.com","from":"me@qq.com","use_tls":false}

`use_tls` false (default) means implicit SSL, typically port 465.
`use_tls` true means STARTTLS after connecting, typically port 587.

Usage:
  python send_mail.py <subject> <body-file>
  python send_mail.py <subject> -        # body is read from stdin
"""

import json
import os
import smtplib
import ssl
import sys
from email.message import EmailMessage
from email.utils import formataddr, formatdate

REQUIRED = ("host", "user", "password", "to")


def log(msg):
    print(f"[mail] {msg}", flush=True)


def load_config(raw):
    if not raw or not raw.strip():
        raise ValueError("MAIL_CONFIG secret is not set")
    try:
        cfg = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"MAIL_CONFIG is not valid JSON: {exc}") from exc
    if not isinstance(cfg, dict):
        raise ValueError("MAIL_CONFIG must be a JSON object")

    missing = [key for key in REQUIRED if not cfg.get(key)]
    if missing:
        raise ValueError(f"MAIL_CONFIG is missing: {', '.join(missing)}")

    cfg["port"] = int(cfg.get("port") or 465)
    cfg["from"] = cfg.get("from") or cfg["user"]
    cfg["use_tls"] = bool(cfg.get("use_tls", False))
    cfg["subject_prefix"] = cfg.get("subject_prefix", "")
    return cfg


def build_message(cfg, subject, body):
    msg = EmailMessage()
    msg["Subject"] = f"{cfg['subject_prefix']}{subject}" if cfg["subject_prefix"] else subject
    msg["From"] = formataddr((cfg.get("from_name", ""), cfg["from"]))
    msg["To"] = cfg["to"]
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(body)
    return msg


def send(cfg, subject, body):
    msg = build_message(cfg, subject, body)
    host, port = cfg["host"], cfg["port"]
    context = ssl.create_default_context()

    if cfg["use_tls"]:
        log(f"connecting to {host}:{port} with STARTTLS")
        with smtplib.SMTP(host, port, timeout=90) as smtp:
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
            smtp.login(cfg["user"], cfg["password"])
            smtp.send_message(msg)
    else:
        log(f"connecting to {host}:{port} with implicit SSL")
        with smtplib.SMTP_SSL(host, port, timeout=90, context=context) as smtp:
            smtp.login(cfg["user"], cfg["password"])
            smtp.send_message(msg)

    log(f"sent to {cfg['to']}")


def main(argv):
    if len(argv) != 3:
        log("usage: send_mail.py <subject> <body-file>")
        return 2

    subject, body_path = argv[1], argv[2]
    try:
        cfg = load_config(os.environ.get("MAIL_CONFIG", ""))
    except ValueError as exc:
        log(f"configuration problem: {exc}")
        return 1

    if not os.path.exists(body_path) and body_path != "-":
        log(f"body file not found: {body_path}")
        return 1

    if body_path == "-":
        body = sys.stdin.read()
    else:
        with open(body_path, "r", encoding="utf-8") as handle:
            body = handle.read()

    try:
        send(cfg, subject, body)
    except smtplib.SMTPAuthenticationError as exc:
        log(f"authentication failed: {exc.smtp_code} {exc.smtp_error!r}")
        log("hint: the password must be the mailbox authorisation code, not the login password")
        return 1
    except smtplib.SMTPException as exc:
        log(f"smtp error: {type(exc).__name__}: {exc}")
        return 1
    except (OSError, ssl.SSLError) as exc:
        log(f"connection error: {type(exc).__name__}: {exc}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
