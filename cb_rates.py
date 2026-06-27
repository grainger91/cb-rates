#!/usr/bin/env python3
"""
Daily Central Bank Policy Rate briefing.

Pulls the latest policy rate for a set of major central banks from the
Bank for International Settlements (BIS) Central Bank Policy Rates dataset
(WS_CBPOL) via the free BIS SDMX REST API, then emails a formatted summary.

No API key needed for BIS. Email goes out via Gmail SMTP using a Google
App Password. See SETUP notes at the bottom of this file.
"""

import os
import io
import csv
import sys
import smtplib
import datetime as dt
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

# --- Which central banks to include -------------------------------------
# BIS ref_area code -> (display name, country/area, what the rate represents)
BANKS = [
    ("US", "Federal Reserve",           "United States",  "Fed funds target (midpoint)"),
    ("XM", "European Central Bank",     "Euro area",      "Deposit facility rate"),
    ("JP", "Bank of Japan",             "Japan",          "Policy rate"),
    ("GB", "Bank of England",           "United Kingdom", "Bank Rate"),
    ("CH", "Swiss National Bank",       "Switzerland",    "Policy rate"),
    ("CA", "Bank of Canada",            "Canada",         "Overnight rate target"),
    ("AU", "Reserve Bank of Australia", "Australia",      "Cash rate target"),
    ("IN", "Reserve Bank of India",     "India",          "Policy repo rate"),
    ("CN", "People's Bank of China",    "China",          "Policy rate"),
    ("BR", "Banco Central do Brasil",   "Brazil",         "Selic target"),
]

BIS_BASE = "https://stats.bis.org/api/v1/data/WS_CBPOL"


def fetch_rates():
    """Return {ref_area: [(date, value), ...]} sorted latest-first."""
    keys = "+".join(code for code, *_ in BANKS)
    since = (dt.date.today() - dt.timedelta(days=200)).isoformat()
    url = f"{BIS_BASE}/D.{keys}/all?startPeriod={since}"
    req = Request(url, headers={
        "Accept": "application/vnd.sdmx.data+csv;version=1.0.0",
        "User-Agent": "cb-rate-briefing/1.0",
    })
    with urlopen(req, timeout=60) as resp:
        raw = resp.read().decode("utf-8")
    return parse_sdmx_csv(raw)


def parse_sdmx_csv(text):
    out = {}
    reader = csv.DictReader(io.StringIO(text))
    cols = reader.fieldnames or []
    area_col = next((c for c in cols if c.upper() in ("REF_AREA", "REFAREA")), None)
    time_col = next((c for c in cols if c.upper() in ("TIME_PERIOD", "TIME")), None)
    val_col  = next((c for c in cols if c.upper() in ("OBS_VALUE", "VALUE")), None)
    if not (area_col and time_col and val_col):
        raise RuntimeError(f"Unexpected columns from BIS: {cols}")
    for row in reader:
        area = row[area_col]
        try:
            val = float(row[val_col])
        except (TypeError, ValueError):
            continue
        out.setdefault(area, []).append((row[time_col], val))
    for area in out:
        out[area].sort(key=lambda x: x[0], reverse=True)
    return out


def summarize(series):
    """current value/date + last change (bps) and the date it took effect."""
    cur_date, cur_val = series[0]
    change_bps, changed_on = None, None
    for i in range(1, len(series)):
        _, v = series[i]
        if abs(v - cur_val) > 1e-9:
            change_bps = round((cur_val - v) * 100)
            changed_on = series[i - 1][0]
            break
    return cur_val, cur_date, change_bps, changed_on


def build_rows(rates):
    rows = []
    for code, name, country, label in BANKS:
        s = rates.get(code, [])
        if not s:
            rows.append((name, country, label, None, None, None, None))
        else:
            val, date, chg, on = summarize(s)
            rows.append((name, country, label, val, date, chg, on))
    return rows


def fmt_change(chg, on):
    if chg is None:
        return "stable (200d)"
    arrow = "\u25b2" if chg > 0 else "\u25bc"
    return f"{arrow} {abs(chg)} bps on {on}"


def render_text(rows, today):
    lines = [f"Central Bank Policy Rates -- {today}", ""]
    for name, country, label, val, date, chg, on in rows:
        if val is None:
            lines.append(f"{name} ({country}): no recent data")
        else:
            lines.append(f"{name} ({country}): {val:.2f}%  [{label}]  "
                         f"as of {date}  ({fmt_change(chg, on)})")
    lines += ["", "Source: BIS Central Bank Policy Rates (WS_CBPOL)."]
    return "\n".join(lines)


def render_html(rows, today):
    cells = []
    for name, country, label, val, date, chg, on in rows:
        if val is None:
            rate, ch, d, color = "\u2014", "no recent data", "", "#888"
        else:
            rate = f"{val:.2f}%"
            ch = fmt_change(chg, on)
            d = date
            color = "#444"
            if chg and chg > 0:
                color = "#b1521d"
            elif chg and chg < 0:
                color = "#4d5a26"
        cells.append(
            "<tr>"
            f"<td style='padding:8px 12px;border-bottom:1px solid #eee'>{name}<br>"
            f"<span style='color:#888;font-size:12px'>{country} &middot; {label}</span></td>"
            f"<td style='padding:8px 12px;border-bottom:1px solid #eee;text-align:right;font-weight:600'>{rate}</td>"
            f"<td style='padding:8px 12px;border-bottom:1px solid #eee;text-align:right;color:{color};font-size:13px'>{ch}</td>"
            f"<td style='padding:8px 12px;border-bottom:1px solid #eee;text-align:right;color:#888;font-size:12px'>{d}</td>"
            "</tr>"
        )
    return (
        "<html><body style=\"font-family:Arial,Helvetica,sans-serif;color:#222\">"
        "<h2 style=\"margin-bottom:2px\">Central Bank Policy Rates</h2>"
        f"<div style=\"color:#888;font-size:13px;margin-bottom:14px\">{today}</div>"
        "<table style=\"border-collapse:collapse;width:100%;max-width:660px\">"
        "<tr style=\"text-align:left;font-size:12px;color:#888\">"
        "<th style='padding:6px 12px'>Bank</th>"
        "<th style='padding:6px 12px;text-align:right'>Rate</th>"
        "<th style='padding:6px 12px;text-align:right'>Last change</th>"
        "<th style='padding:6px 12px;text-align:right'>As of</th></tr>"
        f"{''.join(cells)}"
        "</table>"
        "<p style=\"color:#888;font-size:12px;margin-top:14px\">"
        "Source: BIS Central Bank Policy Rates (WS_CBPOL). \"Last change\" is the most "
        "recent move within the past ~200 days, so a rate decision shows up the morning after.</p>"
        "</body></html>"
    )


def send_email(subject, text_body, html_body):
    sender    = os.environ["GMAIL_ADDRESS"]
    password  = os.environ["GMAIL_APP_PASSWORD"]
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


def main():
    today = dt.date.today().strftime("%A, %B %d, %Y")
    try:
        rates = fetch_rates()
    except (HTTPError, URLError, RuntimeError) as e:
        body = f"Could not pull rates from BIS today.\n\nError: {e}\n"
        send_email("Central bank rate briefing -- fetch failed", body, f"<pre>{body}</pre>")
        sys.exit(1)
    rows = build_rows(rates)
    text_body = render_text(rows, today)
    html_body = render_html(rows, today)
    send_email(f"Central bank rates -- {dt.date.today().isoformat()}", text_body, html_body)
    print("Sent:\n" + text_body)


if __name__ == "__main__":
    main()
