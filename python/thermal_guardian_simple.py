#!/usr/bin/env python3
"""
iDRAC Thermal Guardian  (Dell PowerEdge R760, iDRAC 9 firmware 7.30.30.30)
==========================================================================

Polls Dell iDRAC controllers over the Redfish API, reads the inlet temperature
sensor, and gracefully shuts down any HOST whose inlet temperature has breached
its critical threshold.

The iDRAC is the out-of-band management controller and is *not* the thing shut
down -- it's the lifeline used to reach the machine. The action targets the HOST
SERVER the iDRAC manages, via a Redfish reset action.

Design property: hard separation between a read-only ASSESSMENT phase (produces
verdicts) and an ACTION phase (acts on breached hosts after a confirmation
re-read). Dictionaries are used throughout as the primary data structure.

Hosts are a DICT KEYED BY ADDRESS. Each value is a self-describing record that
carries everything specific to that host (Redfish ids, per-host threshold, and
room for future per-host options). Shared credentials/timeouts live in CONFIG.

Endpoints are pinned to the R760 / iDRAC 9 7.30.30.30 layout, where the system
and chassis both use the fixed id "System.Embedded.1" and the legacy
Chassis/.../Thermal resource (with its Temperatures array) is present.

TLS verification is disabled (iDRACs ship with self-signed certs).

Each function is deliberately written with plain, step-by-step logic (no
ternaries or compound conditions), even where that takes a few more lines.
"""

import os
import sys
import time
import logging

import requests
import urllib3


# --------------------------------------------------------------------------- #
# Config  (shared across all hosts)
# --------------------------------------------------------------------------- #

CONFIG = {
    "username":      os.environ.get("IDRAC_USER", "root"),
    "password":      os.environ.get("IDRAC_PASS"),   # from env, never source
    "timeout":       10,          # per-request seconds
    "poll_interval": 30,          # seconds to wait before confirmation re-read
    "temp_limit":    43.0,        # inlet fallback (C); prefer per-sensor threshold
    "shutdown_wait": 120,         # seconds to wait for graceful shutdown to take
    "dry_run":       True,        # default True -- action phase only logs
}

# Only the inlet (ambient airflow) sensor is monitored.
INLET_SENSOR = "System Board Inlet Temp"


def make_host(address, system_id="System.Embedded.1",
              chassis_id="System.Embedded.1", temp_limit=None):
    """Host-record factory -- fixed shape every time, so keys never drift.

    `temp_limit` here is a per-host override; None means fall back to
    CONFIG["temp_limit"] (which is itself only a fallback for a sensor that
    doesn't report its own UpperThresholdCritical).
    """
    return {
        "address":    address,
        "system_id":  system_id,
        "chassis_id": chassis_id,
        "temp_limit": temp_limit,
        "ssh_backup": None,        # reserved for a future per-host OS backstop
    }


# Dict keyed by address -> self-describing host record.
hosts = {
    "10.0.0.11": make_host("10.0.0.11"),
    "10.0.0.12": make_host("10.0.0.12"),
}


log = logging.getLogger("thermal_guardian")


# --------------------------------------------------------------------------- #
# Connectivity / health
# --------------------------------------------------------------------------- #

def is_reachable(host, config):
    """GET the service root; confirms the iDRAC responds."""
    addr = host["address"]
    try:
        resp = requests.get(
            f"https://{addr}/redfish/v1/",
            auth=(config["username"], config["password"]),
            timeout=config["timeout"],
            verify=False,
        )
    except requests.RequestException as exc:
        log_event(host, f"reachability check failed: {exc}", "warning")
        return False

    if resp.status_code == 200:
        return True
    return False


def is_authenticated(host, config):
    """A GET requiring auth; distinguishes bad creds (401) from a down host."""
    addr = host["address"]
    try:
        resp = requests.get(
            f"https://{addr}/redfish/v1/Systems/{host['system_id']}",
            auth=(config["username"], config["password"]),
            timeout=config["timeout"],
            verify=False,
        )
    except requests.RequestException as exc:
        log_event(host, f"auth check failed: {exc}", "warning")
        return False

    if resp.status_code == 401:
        log_event(host, "authentication failed (401)", "error")
        return False
    if resp.status_code == 200:
        return True
    return False


def get_power_state(host, config):
    """GET the system resource and return its PowerState string, or None."""
    addr = host["address"]
    try:
        resp = requests.get(
            f"https://{addr}/redfish/v1/Systems/{host['system_id']}",
            auth=(config["username"], config["password"]),
            timeout=config["timeout"],
            verify=False,
        )
    except requests.RequestException as exc:
        log_event(host, f"power state read failed: {exc}", "warning")
        return None

    if resp.status_code != 200:
        return None
    return resp.json().get("PowerState")


def is_powered_on(host, config):
    """True only if the system reports PowerState 'On'."""
    state = get_power_state(host, config)
    if state == "On":
        return True
    return False


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #

def get_temperatures(host, config):
    """GET Chassis/{id}/Thermal and return the Temperatures array."""
    addr = host["address"]
    try:
        resp = requests.get(
            f"https://{addr}/redfish/v1/Chassis/{host['chassis_id']}/Thermal",
            auth=(config["username"], config["password"]),
            timeout=config["timeout"],
            verify=False,
        )
    except requests.RequestException as exc:
        log_event(host, f"get_temperatures failed: {exc}", "warning")
        return []

    if resp.status_code != 200:
        return []
    return resp.json().get("Temperatures", [])


def extract_readings(temps):
    """Reduce a Temperatures array to { sensor_name: { celsius, critical } },
    keeping only the inlet (ambient airflow) sensor."""
    readings = {}
    for sensor in temps:
        name = sensor.get("Name")
        if name != INLET_SENSOR:
            continue

        celsius = sensor.get("ReadingCelsius")
        if celsius is None:
            continue

        readings[name] = {
            "celsius":  celsius,
            "critical": sensor.get("UpperThresholdCritical"),
        }
    return readings


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #

def find_breached(readings, limit):
    """Compare each reading against its own critical threshold (falling back to
    `limit`); return the list of breached sensor names."""
    breached = []
    for name, reading in readings.items():
        threshold = reading.get("critical")
        if threshold is None:
            threshold = limit

        if reading["celsius"] >= threshold:
            breached.append(name)
    return breached


def host_limit(host, config):
    """Per-host threshold override, falling back to the shared CONFIG value."""
    if host["temp_limit"] is not None:
        return host["temp_limit"]
    return config["temp_limit"]


def new_verdict(host):
    """Verdict factory -- fixed shape every time, so keys never drift."""
    return {
        "address":    host["address"],
        "reachable":  False,
        "powered_on": False,
        "readings":   {},
        "breached":   [],
        "action":     "none",   # "none" | "graceful" | "force"
    }


def log_readings(host, readings):
    """Log each sensor reading on its own line."""
    for name, reading in readings.items():
        celsius = reading["celsius"]
        critical = reading["critical"]
        log_event(host, f"{name} = {celsius}C (crit {critical})", "info")


def evaluate_host(host, config):
    """Orchestrate one host: reachable -> powered on -> read -> evaluate.

    Returns a verdict value; does NOT mutate shared state.
    """
    verdict = new_verdict(host)

    if not is_reachable(host, config):
        log_event(host, "unreachable", "warning")
        return verdict
    verdict["reachable"] = True

    if not is_authenticated(host, config):
        return verdict

    if not is_powered_on(host, config):
        log_event(host, "powered off; nothing to do", "info")
        return verdict
    verdict["powered_on"] = True

    temps = get_temperatures(host, config)
    readings = extract_readings(temps)
    limit = host_limit(host, config)

    verdict["readings"] = readings
    verdict["breached"] = find_breached(readings, limit)

    log_readings(host, readings)

    if verdict["breached"]:
        verdict["action"] = "graceful"
        names = ", ".join(verdict["breached"])
        log_event(host, f"BREACHED sensors: {names}", "error")

    return verdict


# --------------------------------------------------------------------------- #
# Action
# --------------------------------------------------------------------------- #

def confirm_still_hot(host, config):
    """Wait poll_interval, re-read; guard against a one-off spike or bad sample."""
    log_event(host, f"confirming breach; waiting {config['poll_interval']}s", "info")
    time.sleep(config["poll_interval"])

    temps = get_temperatures(host, config)
    readings = extract_readings(temps)
    limit = host_limit(host, config)
    breached = find_breached(readings, limit)

    if breached:
        names = ", ".join(breached)
        log_event(host, f"breach confirmed: {names}", "error")
        return True

    log_event(host, "breach did not persist; standing down", "info")
    return False


def reset_host(host, config, reset_type):
    """POST ComputerSystem.Reset with the given ResetType. Returns bool."""
    addr = host["address"]
    try:
        resp = requests.post(
            f"https://{addr}/redfish/v1/Systems/{host['system_id']}/Actions/ComputerSystem.Reset",
            json={"ResetType": reset_type},
            auth=(config["username"], config["password"]),
            timeout=config["timeout"],
            verify=False,
        )
    except requests.RequestException as exc:
        log_event(host, f"{reset_type} failed: {exc}", "error")
        return False

    if resp.status_code in (200, 202, 204):
        return True
    return False


def graceful_shutdown(host, config):
    """POST ComputerSystem.Reset with GracefulShutdown."""
    if config["dry_run"]:
        log_event(host, "DRY-RUN: WOULD graceful-shutdown", "warning")
        return True

    ok = reset_host(host, config, "GracefulShutdown")
    if ok:
        log_event(host, "graceful shutdown issued", "info")
    else:
        log_event(host, "graceful shutdown FAILED", "error")
    return ok


def force_shutdown(host, config):
    """Same action with ForceOff; fallback only."""
    if config["dry_run"]:
        log_event(host, "DRY-RUN: WOULD force-off", "warning")
        return True

    ok = reset_host(host, config, "ForceOff")
    if ok:
        log_event(host, "force-off issued", "info")
    else:
        log_event(host, "force-off FAILED", "error")
    return ok


def confirm_powered_off(host, config):
    """Re-check PowerState; escalate if still on."""
    if config["dry_run"]:
        log_event(host, "DRY-RUN: skipping power-off confirmation", "info")
        return True

    deadline = time.time() + config["shutdown_wait"]
    while time.time() < deadline:
        if not is_powered_on(host, config):
            log_event(host, "confirmed powered off", "info")
            return True
        time.sleep(5)

    log_event(host, f"still powered on after {config['shutdown_wait']}s", "warning")
    return False


def shutdown(host, config):
    """Graceful before force. Escalates to ForceOff only if graceful doesn't take."""
    graceful_shutdown(host, config)
    if confirm_powered_off(host, config):
        return True

    log_event(host, "graceful shutdown did not take; escalating to force-off",
              "warning")
    force_shutdown(host, config)
    return confirm_powered_off(host, config)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def monitor_all(hosts, config):
    """Owns the aggregate dict; stores each returned verdict keyed by address."""
    results = {}
    for address, host in hosts.items():
        results[address] = evaluate_host(host, config)
    return results


def notify(host, verdict):
    """Alert a human before/alongside a shutdown."""
    addr = host["address"]
    action = verdict["action"]
    names = ", ".join(verdict["breached"])
    log_event(host, f"NOTIFY: breach on {addr} -> action {action}; sensors: {names}",
              "warning")


def log_event(host, message, level="info"):
    """Record every reading, decision, and action. `host` may be a record dict
    or a bare address string."""
    if isinstance(host, dict):
        addr = host["address"]
    else:
        addr = host

    logger = getattr(log, level, log.info)
    logger("[%s] %s", addr, message)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
    )
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    if not CONFIG["password"]:
        log.error("IDRAC_PASS is not set; refusing to run without credentials.")
        return 1

    log.info("Thermal Guardian starting (dry_run=%s)", CONFIG["dry_run"])

    results = monitor_all(hosts, CONFIG)

    for address, verdict in results.items():
        if verdict["action"] != "graceful":
            continue

        host = hosts[address]                      # O(1) lookup by address
        if confirm_still_hot(host, CONFIG):
            notify(host, verdict)
            shutdown(host, CONFIG)

    log.info("Thermal Guardian run complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
