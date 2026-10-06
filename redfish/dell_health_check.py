#!/usr/bin/env python3
"""
Dell PowerEdge R760 health check over the iDRAC9 Redfish API (iDRAC firmware 7.x).
"""

import getpass
import requests

# iDRACs ship with self-signed certs, so silence the InsecureRequestWarning noise
requests.packages.urllib3.disable_warnings()

IDRAC_IPS = [
    "10.0.0.101",
    "10.0.0.102",
    "10.0.0.103",
]

TIMEOUT = 20
SEL_ENTRIES_TO_SCAN = 50
SEL_ENTRIES_TO_SHOW = 5

SYSTEM = "/redfish/v1/Systems/System.Embedded.1"
CHASSIS = "/redfish/v1/Chassis/System.Embedded.1"
MANAGER = "/redfish/v1/Managers/iDRAC.Embedded.1"

# Dell OEM rollup attributes exposed under Oem.Dell.DellSystem on iDRAC9
DELL_ROLLUPS = {
    "CPURollupStatus": "CPU",
    "SysMemPrimaryStatus": "Memory",
    "StorageRollupStatus": "Storage",
    "PSRollupStatus": "Power Supplies",
    "FanRollupStatus": "Fans",
    "TempRollupStatus": "Temperature",
    "VoltRollupStatus": "Voltage",
    "CurrentRollupStatus": "Current",
    "BatteryRollupStatus": "CMOS Battery",
    "IntrusionRollupStatus": "Chassis Intrusion",
    "SELRollupStatus": "System Event Log",
    "LicensingRollupStatus": "Licensing",
}


def get(session, ip, path):
    """GET a Redfish URI and return parsed JSON (raises on HTTP errors)."""
    resp = session.get(f"https://{ip}{path}", timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def status_of(obj):
    """Return (health, state) from a Redfish Status block, preferring HealthRollup."""
    st = obj.get("Status") or {}
    return st.get("HealthRollup") or st.get("Health"), st.get("State")


def is_ok(health):
    return health is None or str(health).upper() == "OK"


def record(result, section, name, health):
    """Print one line and log anything that isn't OK as an issue."""
    marker = "   " if is_ok(health) else "!! "
    print(f"   {marker}{str(name):<45} {health or 'N/A'}")
    if not is_ok(health):
        result["issues"].append(f"{section}: {name} = {health}")


def check_server(ip, user, pwd):
    result = {"ip": ip, "issues": []}

    with requests.Session() as s:
        s.auth = (user, pwd)
        s.verify = False
        s.headers.update({"Accept": "application/json"})

        try:
            sysinfo = get(s, ip, SYSTEM)
            mgr = get(s, ip, MANAGER)
        except Exception as e:
            print(f"   FAILED to connect: {e}")
            result["error"] = str(e)
            return result

        health, _ = status_of(sysinfo)
        model = sysinfo.get("Model", "N/A")
        fw = mgr.get("FirmwareVersion", "N/A")
        cpu = sysinfo.get("ProcessorSummary", {})
        mem = sysinfo.get("MemorySummary", {})
        result.update(model=model, tag=sysinfo.get("SKU", "N/A"), health=health or "N/A")

        print(f"   Model        : {model}")
        print(f"   Service Tag  : {result['tag']}")
        print(f"   Hostname     : {sysinfo.get('HostName') or 'N/A'}")
        print(f"   Power State  : {sysinfo.get('PowerState', 'N/A')}")
        print(f"   BIOS         : {sysinfo.get('BiosVersion', 'N/A')}")
        print(f"   iDRAC FW     : {fw}")
        print(f"   CPUs         : {cpu.get('Count', 'N/A')} x {cpu.get('Model', 'N/A')}")
        print(f"   Memory       : {mem.get('TotalSystemMemoryGiB', 'N/A')} GiB")
        print(f"   Overall      : {result['health']}")
        if "R760" not in model:
            print("   NOTE: not an R760 - results may still work but are untested")
        if not str(fw).startswith("7."):
            print(f"   NOTE: iDRAC firmware {fw} - script targets 7.x")

        # ---- Dell component rollups ----------------------------------------
        print("\n  Component rollup (Dell OEM)")
        try:
            dell = sysinfo.get("Oem", {}).get("Dell", {}).get("DellSystem")
            if not dell:
                dell = get(s, ip, f"{SYSTEM}/Oem/Dell/DellSystem/System.Embedded.1")
            for key, label in DELL_ROLLUPS.items():
                if key in dell:
                    record(result, "Rollup", label, dell[key])
        except Exception as e:
            print(f"   Could not read Dell rollups: {e}")

        # ---- Power supplies -------------------------------------------------
        print("\n  Power supplies")
        try:
            power = get(s, ip, f"{CHASSIS}/Power")
            for psu in power.get("PowerSupplies", []):
                psu_health, state = status_of(psu)
                if state == "Absent":
                    continue
                name = f"{psu.get('Name')} ({psu.get('PowerCapacityWatts', '?')}W)"
                record(result, "PSU", name, psu_health)
            ctl = power.get("PowerControl") or [{}]
            print(f"      Current draw: {ctl[0].get('PowerConsumedWatts', 'N/A')} W")
        except Exception as e:
            print(f"   Could not read power: {e}")

        # ---- Fans and temperatures -----------------------------------------
        print("\n  Thermal")
        try:
            thermal = get(s, ip, f"{CHASSIS}/Thermal")
            fans = [f for f in thermal.get("Fans", []) if status_of(f)[1] != "Absent"]
            bad_fans = [f for f in fans if not is_ok(status_of(f)[0])]
            print(f"      Fans OK: {len(fans) - len(bad_fans)}/{len(fans)}")
            for f in bad_fans:
                record(result, "Fan", f.get("Name"), status_of(f)[0])
            for t in thermal.get("Temperatures", []):
                t_health, state = status_of(t)
                if state == "Absent" or t.get("ReadingCelsius") is None:
                    continue
                record(result, "Temp", f"{t.get('Name')} ({t['ReadingCelsius']} C)", t_health)
        except Exception as e:
            print(f"   Could not read thermal: {e}")

        # ---- Storage controllers and drives --------------------------------
        print("\n  Storage")
        try:
            storage = get(s, ip, f"{SYSTEM}/Storage")
            for member in storage.get("Members", []):
                ctrl = get(s, ip, member["@odata.id"])
                record(result, "Controller", ctrl.get("Name", ctrl.get("Id")), status_of(ctrl)[0])
                for d in ctrl.get("Drives", []):
                    drive = get(s, ip, d["@odata.id"])
                    size_gb = round((drive.get("CapacityBytes") or 0) / 1e9)
                    life = drive.get("PredictedMediaLifeLeftPercent")
                    label = f"  {drive.get('Name')} {drive.get('MediaType', '')} {size_gb}GB"
                    if life is not None:
                        label += f" life {life}%"
                    record(result, "Drive", label, status_of(drive)[0])
                    if life is not None and life < 10:
                        result["issues"].append(f"Drive: {drive.get('Name')} wear-out ({life}% left)")
        except Exception as e:
            print(f"   Could not read storage: {e}")

        # ---- Recent SEL warnings / critical events --------------------------
        print(f"\n  SEL (warning/critical in last {SEL_ENTRIES_TO_SCAN} entries)")
        try:
            sel = get(s, ip, f"{MANAGER}/LogServices/Sel/Entries")
            entries = sel.get("Members", [])[:SEL_ENTRIES_TO_SCAN]
            alerts = [x for x in entries if x.get("Severity") in ("Warning", "Critical")]
            if not alerts:
                print("      None")
            for x in alerts[:SEL_ENTRIES_TO_SHOW]:
                print(f"      [{x.get('Severity')}] {x.get('Created')} {x.get('Message')}")
        except Exception as e:
            print(f"   Could not read SEL: {e}")

    return result


def main():
    print("Dell PowerEdge R760 / iDRAC9 7.x Health Check")
    user = input("iDRAC username [root]: ").strip() or "root"
    pwd = getpass.getpass("iDRAC password: ")

    results = []
    for ip in IDRAC_IPS:
        print(f"\n{'=' * 70}\n {ip}\n{'=' * 70}")
        try:
            results.append(check_server(ip, user, pwd))
        except Exception as e:
            print(f"   Unexpected error: {e}")
            results.append({"ip": ip, "issues": [], "error": str(e)})

    # ---- Summary ------------------------------------------------------------
    print(f"\n{'=' * 90}\nSUMMARY\n{'=' * 90}")
    print(f"{'iDRAC IP':<16}{'Model':<22}{'Svc Tag':<10}{'Health':<10}Issues")
    for r in results:
        if r.get("error"):
            print(f"{r['ip']:<16}{'UNREACHABLE':<22}{'-':<10}{'-':<10}{r['error'][:40]}")
            continue
        print(f"{r['ip']:<16}{r['model']:<22}{r['tag']:<10}{r['health']:<10}{len(r['issues'])}")
        for issue in r["issues"]:
            print(f"{'':<16}-> {issue}")


if __name__ == "__main__":
    main()
