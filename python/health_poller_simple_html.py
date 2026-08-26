#!/usr/bin/env python3
"""
iDRAC Health Poller (simple)  (R760, iDRAC 9 firmware 7.30.30.30)
=================================================================

Read-only. For each iDRAC, GET the system resource and print power state plus
overall health, then a warning for any subsystem that isn't OK. One request
per host.

Also writes an HTML report ("health_report.html") to the working directory, so
you can glance at a page instead of logging into each iDRAC.

Endpoint: /redfish/v1/Systems/System.Embedded.1

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


USERNAME = os.environ.get("IDRAC_USER", "root")
PASSWORD = os.environ.get("IDRAC_PASS")
TIMEOUT  = 10
HTML_FILE = "health_report.html"   # written to the working directory

hosts = [
    "10.0.0.11",
    "10.0.0.12",
]

log = logging.getLogger("health_poller")


def get_system(address):
    """GET the system resource for one iDRAC; return the JSON dict, or None."""
    url = f"https://{address}/redfish/v1/Systems/System.Embedded.1"
    try:
        resp = requests.get(
            url,
            auth=(USERNAME, PASSWORD),
            timeout=TIMEOUT,
            verify=False,
        )
    except requests.RequestException as exc:
        log.warning("[%s] system read failed: %s", address, exc)
        return None

    if resp.status_code != 200:
        log.warning("[%s] system returned status %s", address, resp.status_code)
        return None

    return resp.json()


def subsystem_health(system):
    """Return { subsystem: health_string } for the major subsystems."""
    health = {}

    health["System"] = system.get("Status", {}).get("HealthRollup", "Unknown")

    proc = system.get("ProcessorSummary", {}).get("Status", {})
    health["Processors"] = proc.get("Health", "Unknown")

    mem = system.get("MemorySummary", {}).get("Status", {})
    health["Memory"] = mem.get("Health", "Unknown")

    return health


def report_host(address):
    """Read one host and log its power state and health. Return a row dict for
    the HTML report (or a row marking the host unreachable)."""
    system = get_system(address)
    if system is None:
        return {
            "host":        address,
            "reachable":   False,
            "power":       "Unknown",
            "health":      {},
        }

    power = system.get("PowerState", "Unknown")
    health = subsystem_health(system)

    log.info("[%s] power=%s health=%s", address, power, health["System"])

    for name, value in health.items():
        if value != "OK":
            log.warning("[%s] subsystem %s health is %s", address, name, value)

    return {
        "host":      address,
        "reachable": True,
        "power":     power,
        "health":    health,
    }


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
.ok   { color: #2e7d32; }
.bad  { color: #c62828; font-weight: 600; }
tr.unreachable { background: #f0f0f0; color: #888; }
"""


def health_cell(value):
    """Return an escaped <td> for a health value, colored OK vs not-OK."""
    safe = html.escape(str(value))
    if value == "OK":
        return f"<td class='ok'>{safe}</td>"
    return f"<td class='bad'>{safe}</td>"


def render_html(rows):
    """Build the full HTML page from the collected rows. Every value is escaped."""
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    parts = []
    parts.append("<!DOCTYPE html>")
    parts.append("<html><head><meta charset='utf-8'>")
    parts.append("<title>iDRAC Health Report</title>")
    parts.append(f"<style>{HTML_STYLE}</style>")
    parts.append("</head><body>")
    parts.append("<h1>iDRAC Health Report</h1>")
    parts.append(f"<div class='meta'>Generated {html.escape(now)} &middot; "
                 f"{len(rows)} hosts</div>")

    parts.append("<table>")
    parts.append("<tr><th>Host</th><th>Power</th><th>System</th>"
                 "<th>Processors</th><th>Memory</th></tr>")

    for row in rows:
        if not row["reachable"]:
            parts.append("<tr class='unreachable'>")
            parts.append(f"<td>{html.escape(row['host'])}</td>")
            parts.append("<td>unreachable</td><td></td><td></td><td></td>")
            parts.append("</tr>")
            continue

        health = row["health"]
        parts.append("<tr>")
        parts.append(f"<td>{html.escape(row['host'])}</td>")
        parts.append(f"<td>{html.escape(str(row['power']))}</td>")
        parts.append(health_cell(health.get("System", "Unknown")))
        parts.append(health_cell(health.get("Processors", "Unknown")))
        parts.append(health_cell(health.get("Memory", "Unknown")))
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
    log.info("wrote %s (%d hosts)", HTML_FILE, len(rows))


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
        row = report_host(address)
        all_rows.append(row)

    write_html(all_rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
