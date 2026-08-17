"""
Daylight-safe NINA-compatibility / Alpaca capability test for the Seestar
S30 Pro's NATIVE Alpaca server.

Read-only except where explicitly opted into (UTCDate write-back,
site-location write-back, and the optional brief axis-movement test).
Never takes an exposure, never slews to a real target, never opens the arm
unless explicitly confirmed. Does NOT implement plate solving -- this only
determines whether the prerequisites for a NINA-style
Slew -> Expose -> Plate Solve -> Sync -> Reslew workflow exist:

  - standard RA/Dec async slew (CanSlewAsync)
  - standard SyncToCoordinates (CanSync)
  - a working native Camera exposure interface
  - accurate, writable UTC
  - known, writable site location

Run via main.py's menu, or standalone: python daylight_capability_test.py
"""

import json
import os
import sys
import time
from datetime import datetime, timezone

from alpaca.device import AlpacaError
from timeutil import parse_alpaca_datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(SCRIPT_DIR, "logs")

RELEVANT_ACTION_KEYWORDS = [
    "sync", "align", "plate", "solve", "calibr", "horizontal", "level",
    "compass", "north", "startup", "initialize", "goto", "slew",
    "location", "time",
]

ALIGNMENT_MODES = {0: "AltAz", 1: "Polar", 2: "GermanPolar"}
EQUATORIAL_SYSTEMS = {0: "Other", 1: "J2000", 2: "J2050", 3: "B1950"}

CAPABILITY_PROPS = [
    ("cansync", "CanSync"), ("cansyncaltaz", "CanSyncAltAz"),
    ("canslew", "CanSlew"), ("canslewasync", "CanSlewAsync"),
    ("canslewaltaz", "CanSlewAltAz"), ("canslewaltazasync", "CanSlewAltAzAsync"),
    ("canpark", "CanPark"), ("canunpark", "CanUnpark"),
    ("canfindhome", "CanFindHome"), ("cansettracking", "CanSetTracking"),
    ("cansetdeclinationrate", "CanSetDeclinationRate"),
    ("cansetrightascensionrate", "CanSetRightAscensionRate"),
]

STATE_PROPS = [
    "connected", "atpark", "athome", "slewing", "tracking",
    "rightascension", "declination", "altitude", "azimuth",
    "siderealtime", "utcdate", "sitelatitude", "sitelongitude", "siteelevation",
]

CAMERA_PROPS = [
    "name", "description", "driverversion", "interfaceversion", "supportedactions",
    "cameraxsize", "cameraysize", "pixelsizex", "pixelsizey", "sensortype",
    "exposuremin", "exposuremax", "canabortexposure", "canstopexposure",
]

FOCUSER_PROPS = [
    "name", "description", "driverversion", "interfaceversion", "supportedactions",
    "position", "maxstep", "maxincrement", "ismoving", "absolute", "temperature",
]

FILTERWHEEL_PROPS = [
    "name", "description", "driverversion", "interfaceversion", "supportedactions",
    "names", "position", "focusoffsets",
]

SWITCH_PROPS = [
    "name", "description", "driverversion", "interfaceversion", "supportedactions", "maxswitch",
]


class _Tee:
    """Writes to multiple streams at once -- lets us mirror everything
    printed during this test into a dedicated .log file alongside the
    normal console output."""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, s):
        for st in self.streams:
            st.write(s)

    def flush(self):
        for st in self.streams:
            st.flush()


def _safe_get(device, prop, **params):
    """Returns (status, value). status is one of SUPPORTED / NOT_IMPLEMENTED
    / ERROR. Never raises."""
    try:
        value = device.get(prop, **params)
        return "SUPPORTED", value
    except AlpacaError as e:
        if e.error_number == 1024:
            return "NOT_IMPLEMENTED", None
        return "ERROR", f"({e.error_number}) {e.error_message}"
    except Exception as e:
        return "ERROR", str(e)


def _fmt(status, value):
    if status == "SUPPORTED":
        return str(value)
    if status == "NOT_IMPLEMENTED":
        return "NOT IMPLEMENTED"
    return f"ERROR {value}"


def _yn(v):
    if v is True:
        return "YES"
    if v is False:
        return "NO"
    return "UNKNOWN"


# Alpaca UTCDate parsing moved to timeutil.parse_alpaca_datetime (shared
# with centering.py) -- kept as a local alias so the rest of this file
# doesn't need touching.
_parse_alpaca_datetime = parse_alpaca_datetime


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

def _section_server(state, report):
    s = state.server
    print("=== ZWO NATIVE ALPACA SERVER ===\n")
    desc = s.description or {}
    print(f"IP:             {s.ip}")
    print(f"Port:           {s.port}")
    print(f"Server name:    {desc.get('ServerName')}")
    print(f"Manufacturer:   {desc.get('Manufacturer')}")
    print(f"Server version: {desc.get('ManufacturerVersion')}")
    print(f"API versions:   {s.api_versions}")
    print("\nDevices:")
    for info in s.configured_devices:
        print(f"{info['DeviceType']} {info['DeviceNumber']}   {info['DeviceName']}")
    report["server"] = {
        "ip": s.ip, "port": s.port,
        "server_name": desc.get("ServerName"), "manufacturer": desc.get("Manufacturer"),
        "server_version": desc.get("ManufacturerVersion"), "api_versions": s.api_versions,
        "devices": s.configured_devices,
    }


def _section_telescope_identity(t, report):
    print("\n=== TELESCOPE IDENTITY ===\n")
    fields = {}
    for prop in ["name", "description", "driverinfo", "driverversion", "interfaceversion"]:
        status, value = _safe_get(t, prop)
        fields[prop] = {"status": status, "value": value}
        print(f"{prop:<18}{_fmt(status, value)}")

    status, value = _safe_get(t, "alignmentmode")
    label = ALIGNMENT_MODES.get(value, "?") if status == "SUPPORTED" else ""
    fields["alignmentmode"] = {"status": status, "value": value, "label": label}
    print(f"{'alignmentmode':<18}{_fmt(status, value)}" + (f"  ({label})" if label else ""))

    status, value = _safe_get(t, "equatorialsystem")
    label = EQUATORIAL_SYSTEMS.get(value, "?") if status == "SUPPORTED" else ""
    fields["equatorialsystem"] = {"status": status, "value": value, "label": label}
    print(f"{'equatorialsystem':<18}{_fmt(status, value)}" + (f"  ({label})" if label else ""))

    report["telescope"]["identity"] = fields


def _section_telescope_capabilities(t, report):
    print("\n=== TELESCOPE CAPABILITIES ===\n")
    caps = {}
    for prop, label in CAPABILITY_PROPS:
        status, value = _safe_get(t, prop)
        caps[prop] = {"status": status, "value": value}
        print(f"{label:<28}{_fmt(status, value)}")

    axis_names = {0: "Primary", 1: "Secondary", 2: "Tertiary"}
    move_axis = {}
    for axis, name in axis_names.items():
        status, value = _safe_get(t, "canmoveaxis", Axis=axis)
        move_axis[axis] = {"status": status, "value": value}
        print(f"{'CanMoveAxis ' + name:<28}{_fmt(status, value)}")
    caps["canmoveaxis"] = move_axis
    report["telescope"]["capabilities"] = caps


def _section_telescope_state(t, report):
    print("\n=== CURRENT TELESCOPE STATE ===\n")
    values = {}
    for prop in STATE_PROPS:
        status, value = _safe_get(t, prop)
        values[prop] = {"status": status, "value": value}
        print(f"{prop:<16}{_fmt(status, value)}")
    report["telescope"]["state"] = values


def _section_utc_test(t, report):
    print("\n=== UTC TEST ===\n")
    laptop_utc = datetime.now(timezone.utc)
    status, seestar_raw = _safe_get(t, "utcdate")
    result = {"laptop_utc": laptop_utc.isoformat(), "seestar_utc_raw": seestar_raw,
              "read_status": status}

    if status != "SUPPORTED":
        print(f"UTCDate read: {_fmt(status, seestar_raw)}")
        result["diff_seconds"] = None
        result["verdict"] = "FAIL"
        print("Result: FAIL (UTCDate not readable)")
        report["telescope"]["time_test"] = result
        return

    seestar_dt = _parse_alpaca_datetime(seestar_raw)
    print(f"Laptop UTC:  {laptop_utc.isoformat(timespec='seconds')}")
    print(f"Seestar UTC: {seestar_raw}")
    if seestar_dt is None:
        print("Difference:  could not parse Seestar UTC")
        verdict = "FAIL"
        diff = None
    else:
        diff = (seestar_dt - laptop_utc).total_seconds()
        print(f"Difference:  {diff:+.1f} seconds")
        verdict = "PASS" if abs(diff) <= 5 else ("WARNING" if abs(diff) <= 60 else "FAIL")
    print(f"\nResult: {verdict}")
    result["diff_seconds"] = diff
    result["verdict"] = verdict

    ans = input("\nTest writing UTCDate? [y/N]: ").strip().lower()
    result["write_test_run"] = ans == "y"
    if ans == "y":
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        print(f"Writing UTCDate = {now_str} ...")
        try:
            t.put("utcdate", UTCDate=now_str)
            result["write_accepted"] = True
        except AlpacaError as e:
            result["write_accepted"] = False
            print(f"  PUT FAILED: ({e.error_number}) {e.error_message}")
        if result["write_accepted"]:
            status2, readback = _safe_get(t, "utcdate")
            result["readback"] = readback
            print(f"Readback:    {readback}")
            if status2 == "SUPPORTED":
                readback_dt = _parse_alpaca_datetime(readback)
                if readback_dt:
                    diff2 = (readback_dt - datetime.now(timezone.utc)).total_seconds()
                    print(f"Difference after write: {diff2:+.1f} seconds")
                    result["diff_after_write_seconds"] = diff2
    report["telescope"]["time_test"] = result


def _section_location_test(t, report):
    print("\n=== SITE LOCATION TEST ===\n")
    lat_status, lat = _safe_get(t, "sitelatitude")
    lon_status, lon = _safe_get(t, "sitelongitude")
    elev_status, elev = _safe_get(t, "siteelevation")
    print(f"Latitude:   {_fmt(lat_status, lat)}")
    print(f"Longitude:  {_fmt(lon_status, lon)}")
    print(f"Elevation:  {_fmt(elev_status, elev)}")

    result = {
        "latitude": {"status": lat_status, "value": lat},
        "longitude": {"status": lon_status, "value": lon},
        "elevation": {"status": elev_status, "value": elev},
    }

    ans = input("\nTest whether site coordinates are writable? [y/N]: ").strip().lower()
    result["write_test_run"] = ans == "y"
    if ans == "y":
        for name, prop, field, status, value in [
            ("latitude", "sitelatitude", "SiteLatitude", lat_status, lat),
            ("longitude", "sitelongitude", "SiteLongitude", lon_status, lon),
            ("elevation", "siteelevation", "SiteElevation", elev_status, elev),
        ]:
            if status != "SUPPORTED":
                print(f"{name.capitalize()} not readable -- skipping write-back test.")
                result[f"{name}_writable"] = "NOT_TESTED"
                continue
            try:
                t.put(prop, **{field: value})
                readback_status, readback_value = _safe_get(t, prop)
                writable = readback_status == "SUPPORTED"
                print(f"{name.capitalize()} write-back (same value {value}): accepted, "
                      f"readback={readback_value}")
                result[f"{name}_writable"] = "YES" if writable else "UNKNOWN"
            except AlpacaError as e:
                print(f"{name.capitalize()} write-back FAILED: ({e.error_number}) {e.error_message}")
                result[f"{name}_writable"] = "NO" if e.error_number == 1024 else "ERROR"
    else:
        result["latitude_writable"] = "NOT_TESTED"
        result["longitude_writable"] = "NOT_TESTED"
        result["elevation_writable"] = "NOT_TESTED"

    print("\n--- Site location summary ---")
    print(f"Latitude readable:          {_yn(lat_status == 'SUPPORTED')}")
    print(f"Longitude readable:         {_yn(lon_status == 'SUPPORTED')}")
    print(f"Elevation readable:         {_yn(elev_status == 'SUPPORTED')}")
    print(f"Latitude writable:          {result.get('latitude_writable')}")
    print(f"Longitude writable:         {result.get('longitude_writable')}")
    print(f"Elevation writable:         {result.get('elevation_writable')}")
    report["telescope"]["location_test"] = result


def _section_sync_check(t, report):
    print("\n=== SYNC CAPABILITY CHECK (not executed) ===\n")
    cansync_status, cansync = _safe_get(t, "cansync")
    cansyncaltaz_status, cansyncaltaz = _safe_get(t, "cansyncaltaz")

    if cansync_status == "SUPPORTED" and cansync:
        print("STANDARD ALPACA SyncToCoordinates IS ADVERTISED (CanSync=True).")
    else:
        print("*** CanSync is NOT True -- standard SyncToCoordinates is NOT advertised. ***")
        print("This blocks an exact reproduction of NINA's Sync+Reslew behavior.")

    if cansyncaltaz_status == "SUPPORTED" and cansyncaltaz:
        print("SyncToAltAz IS ALSO advertised (CanSyncAltAz=True).")
    else:
        print("SyncToAltAz is NOT advertised (CanSyncAltAz not True).")

    print("\n(No Sync command was sent -- this is a capability check only.)")
    report["telescope"]["sync_check"] = {
        "cansync": {"status": cansync_status, "value": cansync},
        "cansyncaltaz": {"status": cansyncaltaz_status, "value": cansyncaltaz},
    }


def _section_supported_actions(t, report):
    print("\n=== SUPPORTED ACTIONS (Telescope) ===\n")
    status, actions = _safe_get(t, "supportedactions")
    if status != "SUPPORTED":
        print(f"SupportedActions: {_fmt(status, actions)}")
        report["telescope"]["supported_actions"] = {"status": status, "value": actions}
        return

    actions = actions or []
    print("All:")
    if actions:
        for a in actions:
            print(f"  - {a}")
    else:
        print("  (none)")

    relevant = [a for a in actions if any(kw in a.lower() for kw in RELEVANT_ACTION_KEYWORDS)]
    print("\nPotentially relevant:")
    if relevant:
        for a in relevant:
            print(f"  - {a}")
    else:
        print("No alignment-related custom Actions found.")

    report["telescope"]["supported_actions"] = {"status": "SUPPORTED", "value": actions}
    report["telescope"]["supported_actions_relevant"] = relevant


def _device_identity_block(device, props, extra_label=""):
    print(f"\n=== {device.label().upper()}{extra_label} ({device.display_name()}) ===\n")
    fields = {}

    # Connecting is non-physical/safe for every device type (it doesn't move
    # anything or start an exposure) and most identity/capability properties
    # on this driver return NotConnected (1031) until you do -- without this
    # the report below would be full of false-negative errors, not real data.
    conn_status, connected_now = _safe_get(device, "connected")
    if conn_status == "SUPPORTED" and not connected_now:
        try:
            device.connected = True
            conn_status, connected_now = _safe_get(device, "connected")
        except AlpacaError as e:
            print(f"  (auto-connect failed: ({e.error_number}) {e.error_message})")

    fields["connected"] = {"status": conn_status, "value": connected_now}
    print(f"{'connected':<18}{_fmt(conn_status, connected_now)}")
    for prop in props:
        status, value = _safe_get(device, prop)
        fields[prop] = {"status": status, "value": value}
        print(f"{prop:<18}{_fmt(status, value)}")
    return fields


def _section_camera(cam):
    fields = _device_identity_block(cam, CAMERA_PROPS)
    return {"device_number": cam.device_number, "name": cam.display_name(), "fields": fields}


def _section_focuser(foc):
    fields = _device_identity_block(foc, FOCUSER_PROPS)
    return {"device_number": foc.device_number, "name": foc.display_name(), "fields": fields}


def _section_filterwheel(fw):
    fields = _device_identity_block(fw, FILTERWHEEL_PROPS)
    return {"device_number": fw.device_number, "name": fw.display_name(), "fields": fields}


def _section_switch(sw):
    fields = _device_identity_block(sw, SWITCH_PROPS)
    switches = []
    maxswitch = fields.get("maxswitch", {})
    if maxswitch.get("status") == "SUPPORTED":
        n = maxswitch.get("value") or 0
        for i in range(n):
            row = {"id": i}
            for label, method in [
                ("name", sw.get_switch_name), ("description", sw.get_switch_description),
                ("can_write", sw.can_write), ("min", sw.min_switch_value),
                ("max", sw.max_switch_value), ("step", sw.switch_step),
            ]:
                try:
                    row[label] = method(i)
                except AlpacaError as e:
                    row[label] = f"ERROR({e.error_number})"
            switches.append(row)
            print(f"  switch[{i}]: {row}")
    return {"device_number": sw.device_number, "name": sw.display_name(),
            "fields": fields, "switches": switches}


def _section_optional_arm_test(t, report):
    print("\n=== OPTIONAL ARM / MOVEMENT TEST ===\n")
    if not t:
        print("No telescope known -- skipping.")
        return
    ans = input("Run optional arm/movement test? [y/N]: ").strip().lower()
    result = {"run": ans == "y"}
    if ans != "y":
        print("Skipped -- no movement commanded.")
        report["telescope"]["arm_movement_test"] = result
        return

    if not t.connected:
        print("Connecting telescope...")
        try:
            t.connected = True
        except AlpacaError as e:
            print(f"Connect FAILED: ({e.error_number}) {e.error_message}")
            result["connect_error"] = str(e)
            report["telescope"]["arm_movement_test"] = result
            return

    can_status, can = _safe_get(t, "canmoveaxis", Axis=1)
    print(f"CanMoveAxis(1) [Secondary] = {_fmt(can_status, can)}")
    result["can_move_axis_1"] = can if can_status == "SUPPORTED" else None
    if can_status != "SUPPORTED" or not can:
        print("Secondary axis MoveAxis not supported -- nothing to test.")
        report["telescope"]["arm_movement_test"] = result
        return

    rates_status, rates = _safe_get(t, "axisrates", Axis=1)
    print(f"AxisRates(1) = {_fmt(rates_status, rates)}")
    result["axis_rates_1"] = rates if rates_status == "SUPPORTED" else None
    if rates_status != "SUPPORTED" or not rates:
        print("No usable AxisRates -- skipping movement.")
        report["telescope"]["arm_movement_test"] = result
        return

    try:
        highest_max = max(float(r["Maximum"]) for r in rates)
    except (KeyError, TypeError, ValueError):
        highest_max = 0.0
    if highest_max <= 0:
        print("No usable positive rate reported -- skipping movement.")
        report["telescope"]["arm_movement_test"] = result
        return
    safe_rate = min(1.0, highest_max)

    ans2 = input(f"Move secondary axis briefly (rate={safe_rate}, ~0.4s) to verify arm "
                 f"response? Watch the physical unit. [y/N]: ").strip().lower()
    result["movement_confirmed"] = ans2 == "y"
    if ans2 != "y":
        print("Skipped -- no movement commanded.")
        report["telescope"]["arm_movement_test"] = result
        return

    before = {p: _safe_get(t, p)[1] for p in ("altitude", "azimuth", "declination", "rightascension")}
    print(f"Before: {before}")
    try:
        t.move_axis(1, safe_rate)
        time.sleep(0.4)
        t.move_axis(1, 0)
        result["move_command_accepted"] = True
    except AlpacaError as e:
        result["move_command_accepted"] = False
        print(f"MoveAxis FAILED: ({e.error_number}) {e.error_message}")
    after = {p: _safe_get(t, p)[1] for p in ("altitude", "azimuth", "declination", "rightascension")}
    print(f"After:  {after}")
    result["before"] = before
    result["after"] = after
    print("\n(Reported coordinate change, if any, is self-reported by the driver --")
    print(" confirm with your own eyes whether the arm actually moved.)")
    report["telescope"]["arm_movement_test"] = result


def _print_and_build_summary(report):
    caps = report["telescope"].get("capabilities", {})

    def cap(name):
        entry = caps.get(name, {})
        return entry.get("value") if entry.get("status") == "SUPPORTED" else None

    cansync = cap("cansync")
    cansyncaltaz = cap("cansyncaltaz")
    canslewasync = cap("canslewasync")
    canslewaltazasync = cap("canslewaltazasync")
    canpark = cap("canpark")
    canunpark = cap("canunpark")

    time_test = report["telescope"].get("time_test", {})
    time_ok = time_test.get("verdict") in ("PASS", "WARNING")

    loc_test = report["telescope"].get("location_test", {})
    site_readable = (loc_test.get("latitude", {}).get("status") == "SUPPORTED" and
                      loc_test.get("longitude", {}).get("status") == "SUPPORTED")

    cameras_ok = len(report["cameras"]) > 0

    print("\n" + "=" * 56)
    print("SEESTAR S30 PRO -- NINA STYLE ALPACA CAPABILITY REPORT")
    print("=" * 56 + "\n")
    print("Native Alpaca server:           PASS")
    print(f"Version:                        {report['server'].get('server_version')}")

    print("\nTELESCOPE")
    print(f"CanSlewAsync:                   {_yn(canslewasync)}")
    print(f"CanSync:                        {_yn(cansync)}")
    print(f"CanSyncAltAz:                   {_yn(cansyncaltaz)}")
    print(f"CanSlewAltAzAsync:              {_yn(canslewaltazasync)}")
    print(f"CanPark:                        {_yn(canpark)}")
    print(f"CanUnpark:                      {_yn(canunpark)}")

    print("\nTIME / LOCATION")
    print(f"UTC readable:                   {_yn(time_test.get('read_status') == 'SUPPORTED')}")
    print(f"UTC verdict:                    {time_test.get('verdict', 'UNKNOWN')}")
    print(f"UTC writable:                   {_yn(time_test.get('write_accepted'))}")
    print(f"Site location readable:         {_yn(site_readable)}")
    print(f"Site latitude writable:         {loc_test.get('latitude_writable', 'NOT_TESTED')}")
    print(f"Site longitude writable:        {loc_test.get('longitude_writable', 'NOT_TESTED')}")

    print("\nCAMERA")
    print(f"Native Camera found:            {_yn(cameras_ok)}")
    print(f"Standard exposure interface:    {_yn(cameras_ok)}")

    slew_ok = bool(canslewasync)
    sync_ok = bool(cansync)

    print("\nNINA-STYLE PREREQUISITES")
    print("-" * 34)
    print(f"Standard RA/Dec slew:           {_yn(slew_ok)}")
    print(f"Standard SyncToCoordinates:     {_yn(sync_ok)}")
    print(f"Native image acquisition:       {_yn(cameras_ok)}")
    print(f"Correct UTC:                    {_yn(time_ok)}")
    print(f"Known site location:            {_yn(site_readable)}")

    if slew_ok and sync_ok and cameras_ok and time_ok and site_readable:
        classification = "likely_viable"
        print("\nRESULT:")
        print("NINA-STYLE SLEW + PLATE SOLVE + SYNC + RESLEW")
        print("APPEARS VIABLE THROUGH STANDARD NATIVE ALPACA.")
        print("\nNight-time validation still required.")
    elif slew_ok and cameras_ok and time_ok and site_readable and not sync_ok:
        classification = "partially_viable_no_sync"
        print("\nRESULT:")
        print("PARTIALLY VIABLE")
        print("\nBlocking difference from normal NINA Slew-and-Center:")
        print("CanSync = False")
        print("\nPossible workaround:")
        print("plate-solve and issue iterative corrected slews without Sync.")
    else:
        classification = "not_viable"
        missing = []
        if not slew_ok:
            missing.append("CanSlewAsync=False")
        if not cameras_ok:
            missing.append("no camera found")
        if not time_ok:
            missing.append("UTC not OK")
        if not site_readable:
            missing.append("site location not readable")
        print("\nRESULT:")
        print("NOT CURRENTLY VIABLE THROUGH STANDARD ALPACA ALONE.")
        print("Blocking issue(s): " + ", ".join(missing))

    return {
        "slew": slew_ok, "sync": sync_ok, "camera": cameras_ok,
        "utc_ok": time_ok, "site_ok": site_readable, "classification": classification,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _run(state):
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "server": {}, "telescope": {}, "cameras": [], "focusers": [],
        "filterwheels": [], "switches": [], "nina_compatibility": {},
    }

    print("=" * 56)
    print("DAYLIGHT ALPACA / NINA COMPATIBILITY TEST")
    print("=" * 56)
    print("\nSafe to run in daylight: read-only except where you explicitly")
    print("opt into a write-back test or the optional arm/movement test.\n")

    _section_server(state, report)

    telescopes = state.by_type("telescope")
    t = telescopes[0] if telescopes else None
    if t:
        if not t.connected:
            print("\nConnecting telescope (read-only properties still work without this, "
                  "but some do not)...")
            try:
                t.connected = True
            except AlpacaError as e:
                print(f"Connect FAILED: ({e.error_number}) {e.error_message}")
        _section_telescope_identity(t, report)
        _section_telescope_capabilities(t, report)
        _section_telescope_state(t, report)
        _section_utc_test(t, report)
        _section_location_test(t, report)
        _section_sync_check(t, report)
        _section_supported_actions(t, report)
    else:
        print("\nNo Telescope device found -- skipping telescope sections.")

    for cam in state.by_type("camera"):
        report["cameras"].append(_section_camera(cam))
    for foc in state.by_type("focuser"):
        report["focusers"].append(_section_focuser(foc))
    for fw in state.by_type("filterwheel"):
        report["filterwheels"].append(_section_filterwheel(fw))
    for sw in state.by_type("switch"):
        report["switches"].append(_section_switch(sw))

    _section_optional_arm_test(t, report)

    report["nina_compatibility"] = _print_and_build_summary(report)
    return report


def run_daylight_capability_test(state):
    if not state.server or not state.devices:
        print("No server/devices known yet -- run Discover first.")
        return None

    os.makedirs(LOGS_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(LOGS_DIR, f"daylight_capability_{ts}.log")

    with open(log_path, "w", encoding="utf-8") as logf:
        old_stdout = sys.stdout
        sys.stdout = _Tee(old_stdout, logf)
        try:
            report = _run(state)
        finally:
            sys.stdout = old_stdout

    json_path = os.path.join(LOGS_DIR, f"daylight_capability_{ts}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)

    print(f"\nSaved human-readable log: {log_path}")
    print(f"Saved machine-readable report: {json_path}")
    return report


if __name__ == "__main__":
    import main as _console

    _console.setup_logging()
    cfg = _console.load_config()
    state = _console.AppState(cfg)
    _console.do_discover(state)
    if state.server:
        run_daylight_capability_test(state)
