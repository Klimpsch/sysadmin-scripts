#!/usr/bin/env python3
"""
iDRAC SEL Collector (simple)  (R760, iDRAC 9 firmware 7.30.30.30)
=================================================================

Read-only. For each iDRAC, GET the System Event Log and print the recent
Warning/Critical entries. One request per host, one page of entries (iDRAC
returns the most recent ~50), no pagination.

Also writes an HTML report ("sel_report.html") to the working directory, so you
can glance at a page instead of logging into each iDRAC.

Endpoint: /redfish/v1/Managers/iDRAC.Embedded.1/LogServices/Sel/Entries

TLS verification is disabled (self-signed certs). Logs to stderr for journald.
Credentials come from IDRAC_USER (default root) and IDRAC_PASS.
"""

import os
import sys
import html
import logging
import datetime

import requests
import urllib3


USERNAME   = os.environ.get("IDRAC_USER", "root")
PASSWORD   = os.environ.get("IDRAC_PASS")
TIMEOUT    = 15
SEVERITIES = ["Warning", "Critical"]
HTML_FILE  = "sel_report.html"     # written to the working directory

hosts = [
    "10.0.0.11",
    "10.0.0.12",
]

log = logging.getLogger("sel_collector")


def get_sel_entries(address):
    """GET the SEL for one iDRAC; return the list of entry dicts (or empty)."""
    url = f"https://{address}/redfish/v1/Managers/iDRAC.Embedded.1/LogServices/Sel/Entries"
    try:
        resp = requests.get(
            url,
            auth=(USERNAME, PASSWORD),
            timeout=TIMEOUT,
            verify=False,
        )
    except requests.RequestException as exc:
        log.warning("[%s] SEL read failed: %s", address, exc)
        return []

    if resp.status_code != 200:
        log.warning("[%s] SEL returned status %s", address, resp.status_code)
        return []

    return resp.json().get("Members", [])


def report_host(address):
    """Read one host's SEL, log its Warning/Critical entries, and return them
    as a list of row dicts for the HTML report."""
    entries = get_sel_entries(address)

    rows = []
    for entry in entries:
        severity = entry.get("Severity")
        if severity not in SEVERITIES:
            continue

        created = entry.get("Created", "?")
        message = entry.get("Message", "")
        log.warning("[%s] %s %s %s", address, created, severity, message)

        rows.append({
            "host":     address,
            "created":  created,
            "severity": severity,
            "message":  message,
        })

    if not rows:
        log.info("[%s] no Warning/Critical SEL entries", address)

    return rows


# --------------------------------------------------------------------------- #
# HTML report
# --------------------------------------------------------------------------- #

HTML_STYLE = """
body { font-family: system-ui, sans-serif; margin: 2rem; color: #1a1a1a; }
h1 { font-size: 1.3rem; }
.meta { color: #666; font-size: 0.85rem; margin-bottom: 1.5rem; }
table { border-collapse: collapse; width: 100%; font-size: 0.9rem; }
th, td { text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid #ddd; }
th { background: #f2f2f2; }
td.msg { font-family: ui-monospace, monospace; }
tr.Critical { background: #fdecea; }
tr.Warning  { background: #fff6e5; }
.none { color: #2e7d32; }
"""


def render_html(rows):
    """Build the full HTML page from the collected rows. Every value is escaped."""
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    parts = []
    parts.append("<!DOCTYPE html>")
    parts.append("<html><head><meta charset='utf-8'>")
    parts.append("<title>iDRAC SEL Report</title>")
    parts.append(f"<style>{HTML_STYLE}</style>")
    parts.append("</head><body>")
    parts.append("<h1>iDRAC SEL Report</h1>")
    parts.append(f"<div class='meta'>Generated {html.escape(now)} &middot; "
                 f"{len(rows)} matching entries</div>")

    if not rows:
        parts.append("<p class='none'>No Warning/Critical entries.</p>")
        parts.append("</body></html>")
        return "\n".join(parts)

    parts.append("<table>")
    parts.append("<tr><th>Host</th><th>Time</th><th>Severity</th><th>Message</th></tr>")
    for row in rows:
        severity = row["severity"]
        parts.append(f"<tr class='{html.escape(severity)}'>")
        parts.append(f"<td>{html.escape(row['host'])}</td>")
        parts.append(f"<td>{html.escape(str(row['created']))}</td>")
        parts.append(f"<td>{html.escape(severity)}</td>")
        parts.append(f"<td class='msg'>{html.escape(str(row['message']))}</td>")
        parts.append("</tr>")
    parts.append("</table>")

    parts.append("</body></html>")
    return "\n".join(parts)


def write_html(rows):
    """Write the HTML report to the working directory."""
    page = render_html(rows)
    try:
        with open(HTML_FILE, "w", encoding="utf-8") as fh:
            fh.write(page)
    except OSError as exc:
        log.error("could not write %s: %s", HTML_FILE, exc)
        return
    log.info("wrote %s (%d entries)", HTML_FILE, len(rows))


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
    )
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    if not PASSWORD:
        log.error("IDRAC_PASS is not set; refusing to run without credentials.")
        return 1

    all_rows = []
    for address in hosts:
        rows = report_host(address)
        for row in rows:
            all_rows.append(row)

    write_html(all_rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
