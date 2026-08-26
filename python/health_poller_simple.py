#!/usr/bin/env python3
"""
iDRAC Health Poller (simple)  (R760, iDRAC 9 firmware 7.30.30.30)
=================================================================

Read-only. For each iDRAC, GET the system resource and print power state plus
overall health, then a warning for any subsystem that isn't OK. One request
per host.

Endpoint: /redfish/v1/Systems/System.Embedded.1

TLS verification is disabled (self-signed certs). Logs to stderr for journald.
Credentials come from IDRAC_USER (default root) and IDRAC_PASS.
"""

import os
import sys
import logging

import requests
import urllib3


USERNAME = os.environ.get("IDRAC_USER", "root")
PASSWORD = os.environ.get("IDRAC_PASS")
TIMEOUT  = 10

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
    """Read one host and log its power state and health."""
    system = get_system(address)
    if system is None:
        return

    power = system.get("PowerState", "Unknown")
    health = subsystem_health(system)

    log.info("[%s] power=%s health=%s", address, power, health["System"])

    for name, value in health.items():
        if value != "OK":
            log.warning("[%s] subsystem %s health is %s", address, name, value)


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
