#!/usr/bin/env python3
"""
Upload Dell firmware (DUP .EXE files) to iDRAC9 via Redfish, then reboot the host
so the updates apply.
"""

import json
import os
import time
from getpass import getpass

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------- Edit these ----------------
IDRAC_IPS = [
    "192.168.1.101",
    "192.168.1.102",
    "192.168.1.103",
]

FIRMWARE_FILES = [
    "/path/to/BIOS_XXXXX_WN64_1.2.3.EXE",
    "/path/to/Network-Firmware_XXXXX_WN64_22.0.0.EXE",
]

USERNAME = "root"
PASSWORD = getpass("iDRAC password: ")
JOB_WAIT_SECONDS = 600  # how long to wait for each upload to be staged

SYSTEM = "/redfish/v1/Systems/System.Embedded.1"
JOBS = "/redfish/v1/Managers/iDRAC.Embedded.1/Jobs"


def upload_firmware(session, base, upload_uri, path):
    """Upload one DUP and schedule it to install on next reboot. Returns the job ID."""
    params = {"Targets": [], "@Redfish.OperationApplyTime": "OnReset"}
    with open(path, "rb") as fw:
        files = {
            "UpdateParameters": (None, json.dumps(params), "application/json"),
            "UpdateFile": (os.path.basename(path), fw, "application/octet-stream"),
        }
        r = session.post(f"{base}{upload_uri}", files=files, timeout=900)
    r.raise_for_status()
    return r.headers["Location"].split("/")[-1]


def wait_until_ready(session, base, job_id):
    """Wait until the job is Scheduled (waiting for reboot) or Completed."""
    deadline = time.time() + JOB_WAIT_SECONDS
    while time.time() < deadline:
        job = session.get(f"{base}{JOBS}/{job_id}", timeout=30).json()
        state = job.get("JobState")
        if state in ("Scheduled", "Completed"):
            return state
        if state in ("Failed", "CompletedWithErrors"):
            raise RuntimeError(f"{job_id} {state}: {job.get('Message')}")
        time.sleep(10)
    raise TimeoutError(f"{job_id} not ready after {JOB_WAIT_SECONDS}s")


def reboot(session, base):
    """Gracefully restart the host, or power it on if it's off."""
    power = session.get(f"{base}{SYSTEM}", timeout=30).json().get("PowerState")
    reset_type = "On" if power == "Off" else "GracefulRestart"
    r = session.post(
        f"{base}{SYSTEM}/Actions/ComputerSystem.Reset",
        json={"ResetType": reset_type},
        timeout=30,
    )
    r.raise_for_status()
    return reset_type


def update_host(ip):
    base = f"https://{ip}"
    with requests.Session() as session:
        session.auth = (USERNAME, PASSWORD)
        session.verify = False  # iDRAC uses a self-signed cert by default

        update_service = session.get(f"{base}/redfish/v1/UpdateService", timeout=30)
        update_service.raise_for_status()
        upload_uri = update_service.json()["MultipartHttpPushUri"]

        scheduled = 0
        for path in FIRMWARE_FILES:
            try:
                print(f"[{ip}] Uploading {os.path.basename(path)} ...")
                job_id = upload_firmware(session, base, upload_uri, path)
                state = wait_until_ready(session, base, job_id)
                print(f"[{ip}] {job_id} is {state}")
                if state == "Scheduled":
                    scheduled += 1
            except Exception as e:
                print(f"[{ip}] ERROR with {os.path.basename(path)}: {e}")

        if scheduled:
            reset_type = reboot(session, base)
            print(f"[{ip}] Reboot sent ({reset_type}) to apply {scheduled} update(s)")
        else:
            print(f"[{ip}] Nothing scheduled, skipping reboot")


def main():
    for ip in IDRAC_IPS:
        try:
            update_host(ip)
        except Exception as e:
            print(f"[{ip}] ERROR: {e}")
    print("Done.")


if __name__ == "__main__":
    main()
