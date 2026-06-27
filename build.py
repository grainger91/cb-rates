#!/usr/bin/env python3
"""
Daily build for the central bank dashboard + event-driven email.

Each run:
  1. Reads the previously committed docs/data.json (yesterday's state), if any.
  2. Pulls full policy-rate history for all banks from the BIS SDMX API.
  3. Compresses each series to change-points (the only dates a rate moved).
  4. Writes the fresh docs/data.json (so the dashboard is always current).
  5. Emails ONLY if a rate actually changed since last run (or on first run).

No API key needed for BIS. Email via Gmail SMTP using a Google App Password.
"""

import os
import io
import csv
import sys
import json
import smtplib
import datetime as dt
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

DATA_PATH = "docs/data.json"
BIS_BASE = "https://stats.bis.org/api/v1/data/WS_CBPOL"

# BIS ref_area -> display metadata, line colour, and 2026 meeting calendar.
BANKS = [
    ("US", "Fed", "Federal Reserve",           "Fed funds target (midpoint)", "#378ADD",
     ["2026-09-16", "2026-10-28", "2026-12-09"]),
    ("XM", "ECB", "European Central Bank",      "Deposit facility rate",       "#1D9E75",
     ["2026-07-23", "2026-09-10", "2026-10-29", "2026-12-17"]),
    ("JP", "BoJ", "Bank of Japan",              "Policy rate",                 "#D85A30",
     ["2026-07-31", "2026-09-18", "2026-10-30", "2026-12-18"]),
    ("GB", "BoE", "Bank of England",            "Bank Rate",                   "#534AB7",
     ["2026-07-30", "2026-09-17", "2026-11-05", "2026-12-17"]),
    ("CH", "SNB", "Swiss National Bank",        "Policy rate",                 "#0F6E56",
     ["2026-09-24", "2026-12-10"]),
    ("CA", "BoC", "Bank of Canada",             "Overnight rate target",       "#BA7517",
     ["2026-07-15", "2026-09-02", "2026-10-28", "2026-12-09"]),
    ("AU", "RBA", "Reserve Bank of Australia",  "Cash rate target",            "#D4537E",
     ["2026-08-11", "2026-09-29", "2026-11-03", "2026-12-08"]),
    ("IN", "RBI", "Reserve Bank of India",      "Policy repo rate",            "#993C1D",
     ["2026-08-05"]),
    ("CN", "PBoC", "People's Bank of China",    "Policy rate",                 "#185FA5",
     []),
    ("BR", "BCB", "Banco Central do Brasil",    "Selic target",                "#3B6D11",
     ["2026-08-05", "2026-09-16", "2026-11-04", "2026-12-09"]),
]


def fetch_history():
    keys = "+".join(code for code, *_ in BANKS)
    url = f"{BIS_BASE}/D.{keys}/all?startPeriod=1980-01-01"
    req = Request(url, headers={
        "Accept": "application/vnd.sdmx.data+csv;version=1.0.0",
        "User-Agent": "cb-dashboard/1.0",
    })
    with urlopen(req, timeout=120) as resp:
        raw = resp.read().decode("utf-8")
    return parse_sdmx_csv(raw)


def parse_sdmx_csv(text):
    reader = csv.DictReader(io.StringIO(text))
    cols = reader.fieldnames or []
    area_col = next((c for c in cols if c.upper() in ("REF_AREA", "REFAREA")), None)
    time_col = next((c for c in cols if c.upper() in ("TIME_PERIOD", "TIME")), None)
    val_col  = next((c for c in cols if c.upper() in ("OBS_VALUE", "VALUE")), None)
    if not (area_col and time_col and val_col):
        raise RuntimeError(f"Unexpected columns from BIS: {cols}")
    raw = {}
    for row in reader:
        try:
            v = float(row[val_col])
        except (TypeError, ValueError):
            continue
        raw.setdefault(row[area_col], []).append((row[time_col], v))
    return raw


def compress(obs):
    obs = sorted(obs, key=lambda x: x[0])
    pts = []
    for date, val in obs:
        if not pts or abs(pts[-1][1] - val) > 1e-9:
            pts.append([date, round(val, 4)])
    return pts


def build_payload(raw):
    today = dt.date.today().isoformat()
    banks = []
    for code, short, name, label, color, meetings in BANKS:
        pts = compress(raw.get(code, []))
        nxt = next((m for m in meetings if m >= today), None)
        banks.append({
            "key": code, "short": short, "name": name, "label": label,
            "color": color, "points": pts, "next_meeting": nxt,
        })
    return {"generated": today, "banks": banks}


def load_old():
    try:
        with open(DATA_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def latest(bank):
    return bank["points"][-1] if bank.get("points") else None


def find_changes(old, new):
    if old is None:
        return None  # first run
    old_latest = {b["key"]: latest(b) for b in old.get("banks", [])}
    changes = []
    for b in new["banks"]:
        nl = latest(b)
        ol = old_latest.get(b["key"])
        if nl and nl != ol:
            prev_val = ol[1] if ol else None
            changes.append((b, prev_val))
    return changes


def fmt_bps(cur, prev):
    if prev is None:
        return "new"
    bps = round((cur - prev) * 100)
    return f"{'+' if bps > 0 else ''}{bps} bps"


def send_email(subject, text_body, html_body):
    sender = os.environ["GMAIL_ADDRESS"]
    password = os.environ["GMAIL_APP_PASSWORD"]
    recipient = os.environ.get("RECIPIENT", sender)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as server:
        server.login(sender, password)
        server.sendmail(sender, [recipient], msg.as_string())


def change_email(changes, new):
    today = dt.date.today().strftime("%A, %B %d, %Y")
    headline = ", ".join(
        f"{b['short']} {fmt_bps(latest(b)[1], prev)} to {latest(b)[1]:.2f}%"
        for b, prev in changes
    )
    tlines = [f"Rate change -- {today}", "", headline, "", "Current levels:"]
    rows = ""
    for b in new["banks"]:
        lv = latest(b)
        if not lv:
            continue
        moved = any(c[0]["key"] == b["key"] for c in changes)
        tlines.append(f"  {b['short']}: {lv[1]:.2f}%  ({lv[0]}){'  <-- changed' if moved else ''}")
        mark = "background:var(--bg);" if moved else ""
        rows += (
            f"<tr style='{'background:#faece7;' if moved else ''}'>"
            f"<td style='padding:6px 12px;border-bottom:1px solid #eee'>{b['short']}"
            f"<span style='color:#888;font-size:12px'> &middot; {b['name']}</span></td>"
            f"<td style='padding:6px 12px;border-bottom:1px solid #eee;text-align:right;font-weight:600'>{lv[1]:.2f}%</td>"
            f"<td style='padding:6px 12px;border-bottom:1px solid #eee;text-align:right;color:#888;font-size:12px'>{lv[0]}</td>"
            "</tr>"
        )
    html = (
        "<html><body style=\"font-family:Arial,Helvetica,sans-serif;color:#222\">"
        f"<h2 style=\"margin-bottom:2px\">Rate change</h2>"
        f"<div style=\"color:#b1521d;font-size:14px;margin-bottom:14px\">{headline}</div>"
        f"<table style=\"border-collapse:collapse;width:100%;max-width:620px\">{rows}</table>"
        "<p style=\"color:#888;font-size:12px;margin-top:14px\">Source: BIS. You only get this email "
        "on days a policy rate moves. The dashboard always shows the live picture.</p>"
        "</body></html>"
    )
    return f"Rate move: {headline}", "\n".join(tlines), html


def baseline_email(new):
    today = dt.date.today().strftime("%A, %B %d, %Y")
    tlines = [f"Central bank dashboard is live -- {today}", "", "Starting levels:"]
    for b in new["banks"]:
        lv = latest(b)
        if lv:
            tlines.append(f"  {b['short']}: {lv[1]:.2f}%  ({lv[0]})")
    tlines += ["", "From now on you'll only be emailed when a rate changes."]
    body = "\n".join(tlines)
    return "Central bank dashboard is live", body, f"<pre style='font-family:Arial'>{body}</pre>"


def main():
    old = load_old()
    raw = fetch_history()
    new = build_payload(raw)

    os.makedirs("docs", exist_ok=True)
    with open(DATA_PATH, "w") as f:
        json.dump(new, f, separators=(",", ":"))

    changes = find_changes(old, new)
    if changes is None:
        subj, text, html = baseline_email(new)
        send_email(subj, text, html)
        print("First run: baseline email sent; data.json written.")
    elif changes:
        subj, text, html = change_email(changes, new)
        send_email(subj, text, html)
        print("Change detected; email sent:\n" + text)
    else:
        print("No rate changes today; data.json refreshed, no email sent.")


if __name__ == "__main__":
    main()
