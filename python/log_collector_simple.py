#!/usr/bin/env python3
"""
iDRAC SEL Collector (simple)  (R760, iDRAC 9 firmware 7.30.30.30)
=================================================================

Read-only. For each iDRAC, GET the System Event Log and print the recent
Warning/Critical entries. One request per host, one page of entries (iDRAC
returns the most recent ~50), no pagination.

Endpoint: /redfish/v1/Managers/iDRAC.Embedded.1/LogServices/Sel/Entries

TLS verification is disabled (self-signed certs). Logs to stderr for journald.
Credentials come from IDRAC_USER (default root) and IDRAC_PASS.
"""

import os
import sys
import logging

import requests
import urllib3


USERNAME   = os.environ.get("IDRAC_USER", "root")
PASSWORD   = os.environ.get("IDRAC_PASS")
TIMEOUT    = 15
SEVERITIES = ["Warning", "Critical"]

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
    """Read one host's SEL and log its Warning/Critical entries."""
    entries = get_sel_entries(address)

    found = 0
    for entry in entries:
        severity = entry.get("Severity")
        if severity not in SEVERITIES:
            continue

        created = entry.get("Created", "?")
        message = entry.get("Message", "")
        log.warning("[%s] %s %s %s", address, created, severity, message)
        found = found + 1

    if found == 0:
        log.info("[%s] no Warning/Critical SEL entries", address)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
    )
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    if not PASSWORD:
        log.error("IDRAC_PASS is not set; refusing to run without credentials.")
        return 1

    for address in hosts:
        report_host(address)

    return 0


if __name__ == "__main__":
    sys.exit(main())
