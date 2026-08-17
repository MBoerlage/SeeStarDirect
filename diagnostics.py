"""
Capability interrogation and verbose diagnostic sequences.

These deliberately query properties one at a time and print exactly what
came back (including ErrorNumber/ErrorMessage on failure) rather than
assuming a fixed capability set -- the whole point is to see what THIS
S30 Pro's Alpaca server actually reports, not what we assume it should.
"""

import re
import time

from alpaca.device import AlpacaError

# ZWO stated the automatic-arm-raise-on-first-GoTo bug was fixed in this
# Alpaca/driver version. Anything reporting older than this gets a loud
# warning instead of silently failing diagnostics.
MIN_GOOD_VERSION = (1, 1, 3, 1)

# Properties queried per device type in the capability report. Every one of
# these was confirmed present (implemented or explicitly NotImplemented) on
# the live S30 Pro on 2026-08-17.
TYPE_SPECIFIC_PROPS = {
    "telescope": [
        "canpark", "canunpark", "canfindhome", "canslew", "canslewasync",
        "cansync", "cansettracking", "canpulseguide", "cansetpierside",
        "cansyncaltaz", "canslewaltaz", "canslewaltazasync",
        "atpark", "slewing", "tracking",
        "rightascension", "declination", "altitude", "azimuth",
        "equatorialsystem", "trackingrates", "utcdate",
    ],
    "camera": [
        "cameraxsize", "cameraysize", "pixelsizex", "pixelsizey",
        "sensortype", "sensorname", "maxadu",
        "exposuremin", "exposuremax", "exposureresolution",
        "canabortexposure", "canstopexposure",
        "cangetcoolerpower", "cansetccdtemperature",
        "gain", "gainmin", "gainmax",
        "binx", "biny", "maxbinx", "maxbiny",
        "imageready", "camerastate",
    ],
    "focuser": [
        "position", "maxstep", "maxincrement", "ismoving", "absolute",
        "tempcomp", "tempcompavailable", "temperature",
    ],
    "filterwheel": [
        "names", "position", "focusoffsets",
    ],
    "switch": [
        "maxswitch",
    ],
}

COMMON_PROPS = ["name", "description", "driverinfo", "driverversion", "interfaceversion"]


def report_action(device, action_name, fn):
    """Runs fn(), printing the exact FAILED/OK format the project spec asks
    for. Returns fn()'s result, or None on failure."""
    try:
        result = fn()
        print(f"[{device.label()}] {action_name} OK")
        return result
    except AlpacaError as e:
        print(f"[{device.label()}] {action_name} FAILED")
        print(f"  Alpaca ErrorNumber: {e.error_number}")
        print(f"  ErrorMessage: {e.error_message}")
        return None
    except Exception as e:  # transport errors etc.
        print(f"[{device.label()}] {action_name} FAILED (transport)")
        print(f"  {e}")
        return None


def try_action(device, action_name, fn):
    """Like report_action, but returns a plain True/False for accepted/failed.

    Needed for PUT methods (Unpark, MoveAxis, ...) whose Alpaca response
    carries no meaningful Value -- report_action's return value would be
    None on success there, indistinguishable from failure."""
    try:
        fn()
        print(f"[{device.label()}] {action_name} OK")
        return True
    except AlpacaError as e:
        print(f"[{device.label()}] {action_name} FAILED")
        print(f"  Alpaca ErrorNumber: {e.error_number}")
        print(f"  ErrorMessage: {e.error_message}")
        return False
    except Exception as e:
        print(f"[{device.label()}] {action_name} FAILED (transport)")
        print(f"  {e}")
        return False


def _print_prop(device, prop):
    try:
        val = device.get(prop)
        print(f"    {prop}: {val}")
    except AlpacaError as e:
        print(f"    {prop}: ERROR ({e.error_number}) {e.error_message}")
    except Exception as e:
        print(f"    {prop}: ERROR (transport) {e}")


def print_capability_report(devices):
    for dev in devices:
        print(f"\n--- {dev.label()} : {dev.display_name()} ---")
        try:
            print(f"    connected: {dev.connected}")
        except AlpacaError as e:
            print(f"    connected: ERROR ({e.error_number}) {e.error_message}")

        for prop in COMMON_PROPS:
            _print_prop(dev, prop)

        for prop in TYPE_SPECIFIC_PROPS.get(dev.device_type, []):
            _print_prop(dev, prop)

        try:
            actions = dev.supportedactions
            print(f"    supportedactions: {actions}")
        except AlpacaError as e:
            print(f"    supportedactions: ERROR ({e.error_number}) {e.error_message}")

        if dev.device_type == "switch":
            _print_switch_detail(dev)


def _print_switch_detail(switch_dev):
    try:
        n = switch_dev.maxswitch
    except AlpacaError as e:
        print(f"    (could not enumerate switches: {e})")
        return
    for i in range(n or 0):
        try:
            name = switch_dev.get_switch_name(i)
            value = switch_dev.get_switch_value(i)
            can_write = switch_dev.can_write(i)
            lo = switch_dev.min_switch_value(i)
            hi = switch_dev.max_switch_value(i)
            step = switch_dev.switch_step(i)
            print(f"    switch[{i}] '{name}' = {value}  (range {lo}-{hi} step {step}, "
                  f"writable={can_write})")
        except AlpacaError as e:
            print(f"    switch[{i}]: ERROR ({e.error_number}) {e.error_message}")


def _fmt_state(state):
    lines = []
    for k, v in state.items():
        lines.append(f"  {k:<10}{v}")
    return "\n".join(lines)


def _telescope_state(telescope):
    try:
        connected = telescope.connected
    except AlpacaError as e:
        return {"Connected": f"ERROR {e}"}
    state = {"Connected": connected}
    if connected:
        for label, prop in (("AtPark", "atpark"), ("Slewing", "slewing"), ("Tracking", "tracking")):
            try:
                state[label] = telescope.get(prop)
            except AlpacaError as e:
                state[label] = f"ERROR ({e.error_number}) {e.error_message}"
    return state


def initialize_telescope(telescope):
    """Verbose, capability-gated 'open arm' sequence.

    We do NOT hard-code a proprietary startup routine. This exercises the
    standard ASCOM Alpaca *park-state* sequence:

        Connected = True
        Unpark()      (only if CanUnpark)
        FindHome()    (only if CanFindHome)

    IMPORTANT: Unpark/FindHome are ASCOM's abstract "leave parked state" /
    "seek home position" concepts -- they are NOT the same thing as the
    Seestar's physical folding arm. Confirmed live on this hardware: both
    calls return ErrorNumber=0 and Slewing briefly toggles True->False, but
    NO physical arm movement was observed. Do not treat this function's
    success as proof the arm opened -- use run_first_slew_test() for that;
    it reproduces what NINA actually does (Connect + GoTo), which is the
    documented trigger for ZWO's automatic arm raise.
    """
    print("=== INITIALIZE TELESCOPE ===\n")
    print(_fmt_state(_telescope_state(telescope)))

    if not telescope.connected:
        print("\nSetting Connected=True...")
        report_action(telescope, "Connected=True", lambda: setattr(telescope, "connected", True))
    print(_fmt_state(_telescope_state(telescope)))

    can_unpark = report_action(telescope, "CanUnpark", lambda: telescope.canunpark)
    if can_unpark:
        print("\nSending Unpark...")
        report_action(telescope, "Unpark", telescope.unpark)
        time.sleep(1)
        print(_fmt_state(_telescope_state(telescope)))
    else:
        print("\nCanUnpark=False -- skipping Unpark (device does not support it).")

    can_find_home = report_action(telescope, "CanFindHome", lambda: telescope.canfindhome)
    if can_find_home:
        print("\nSending FindHome...")
        report_action(telescope, "FindHome", telescope.find_home)
        print("Waiting for Slewing to clear (up to 60s)...")
        for _ in range(60):
            time.sleep(1)
            try:
                if not telescope.get("slewing"):
                    break
            except AlpacaError:
                break
        print(_fmt_state(_telescope_state(telescope)))
    else:
        print("\nCanFindHome=False -- skipping FindHome (device does not support it).")

    print("\n=== INITIALIZE COMPLETE ===")
    print("Unpark/FindHome success here does NOT confirm the arm physically opened.")
    print("Run the Auto Start / First Slew Test (menu 18) to reproduce what NINA does")
    print("(Connect + GoTo), which is the documented trigger for the arm raise.")


# ---------------------------------------------------------------------------
# Version reporting -- distinguishes standard ASCOM behavior from the known
# ZWO Alpaca 1.1.x "arm doesn't auto-raise on first GoTo" issue, fixed in
# 1.1.3-1 per ZWO.
# ---------------------------------------------------------------------------

def parse_version(v):
    """'1.1.1-1' -> (1, 1, 1, 1). Best-effort: pulls out all integer runs."""
    nums = re.findall(r"\d+", str(v))
    return tuple(int(n) for n in nums) if nums else (0,)


def version_is_older(v, threshold=MIN_GOOD_VERSION):
    pv = parse_version(v)
    n = max(len(pv), len(threshold))
    pv = pv + (0,) * (n - len(pv))
    th = threshold + (0,) * (n - len(threshold))
    return pv < th


def print_software_info(server, telescope):
    """Prints every version-identifying field we can read, and flags
    versions older than MIN_GOOD_VERSION with the known-issue warning.
    Returns a dict for use in the final diagnostic summary."""
    print("=== SEESTAR SOFTWARE INFORMATION ===\n")

    desc = server.description or {}
    server_version = desc.get("ManufacturerVersion", "?")
    print(f"Server name:            {desc.get('ServerName')}")
    print(f"Server manufacturer:    {desc.get('Manufacturer')}")
    print(f"Server version:         {server_version}")

    fields = {}
    for label, prop in [("driverversion", "driverversion"), ("driverinfo", "driverinfo"),
                         ("description", "description"), ("interfaceversion", "interfaceversion")]:
        try:
            fields[label] = telescope.get(prop)
        except AlpacaError as e:
            fields[label] = f"ERROR ({e.error_number}) {e.error_message}"

    print(f"\nTelescope driver version: {fields['driverversion']}")
    print(f"Telescope driver info:    {fields['driverinfo']}")
    print(f"Telescope description:    {fields['description']}")
    print(f"Telescope interface ver.: {fields['interfaceversion']}")

    check_version = fields["driverversion"] if isinstance(fields["driverversion"], str) \
        and not fields["driverversion"].startswith("ERROR") else server_version
    is_old = isinstance(check_version, str) and version_is_older(check_version)

    if is_old:
        print(f"""
WARNING:
This Seestar appears to be running an older Alpaca implementation
({check_version}, older than 1.1.3-1).

ZWO fixed an issue in Alpaca 1.1.3-1 where external Alpaca clients
could connect but could fail to automatically raise the arm when
starting a GoTo without first connecting the Seestar mobile app.

Update the Seestar firmware/app before debugging Python further.
""")

    return {
        "server_version": server_version,
        "driverversion": fields["driverversion"],
        "checked_version": check_version,
        "is_old_version": is_old,
    }


# ---------------------------------------------------------------------------
# Auto Start / First Slew Test -- reproduces Connect + first GoTo, which is
# the documented trigger for ZWO's automatic arm raise (NOT Unpark/FindHome).
# ---------------------------------------------------------------------------

_SNAPSHOT_PROPS = [
    ("AtPark", "atpark"), ("AtHome", "athome"), ("Slewing", "slewing"),
    ("Tracking", "tracking"), ("RightAscension", "rightascension"),
    ("Declination", "declination"), ("Altitude", "altitude"), ("Azimuth", "azimuth"),
]


def _telescope_snapshot(telescope):
    snap = {}
    for label, prop in _SNAPSHOT_PROPS:
        try:
            snap[label] = telescope.get(prop)
        except AlpacaError:
            snap[label] = None
    return snap


def _fmt_snapshot(snap):
    return "\n".join(f"{k + ':':<16}{v}" for k, v in snap.items())


def _coords_changed(before, after, epsilon=0.05):
    for key in ("Altitude", "Azimuth", "RightAscension", "Declination"):
        b, a = before.get(key), after.get(key)
        if b is None or a is None:
            continue
        try:
            if abs(float(a) - float(b)) > epsilon:
                return True
        except (TypeError, ValueError):
            if a != b:
                return True
    return False


def run_first_slew_test(telescope, ra=None, dec=None):
    """Connect -> read state -> Unpark (only if needed/supported) -> a real
    SlewToCoordinatesAsync -> poll to completion. Deliberately does NOT call
    FindHome -- this reproduces what NINA does on first connect + GoTo, not
    a manual homing routine.

    If ra/dec are given, they're used directly (no console prompt) -- this
    lets non-console callers (e.g. the GUI) reuse this exact routine."""
    results = {}
    print("=== AUTO START / FIRST SLEW TEST ===\n")

    if not telescope.connected:
        print("Connecting...")
        try_action(telescope, "Connect", lambda: setattr(telescope, "connected", True))
    results["connected"] = telescope.connected

    print("\n--- Capability check ---")
    for key, prop in [("canunpark", "canunpark"), ("canfindhome", "canfindhome"),
                       ("canslew", "canslew"), ("canslewasync", "canslewasync"),
                       ("cansync", "cansync"), ("cansettracking", "cansettracking"),
                       ("canslewaltazasync", "canslewaltazasync")]:
        results[key] = report_action(telescope, key, lambda p=prop: telescope.get(p))

    before = _telescope_snapshot(telescope)
    print("\n--- Initial state ---")
    print(_fmt_snapshot(before))
    results["before"] = before

    if results.get("canunpark"):
        print("\nCanUnpark=True -- sending Unpark (AtPark is currently "
              f"{before.get('AtPark')})...")
        results["unpark_accepted"] = try_action(telescope, "Unpark", telescope.unpark)
    else:
        print("\nCanUnpark=False -- skipping Unpark.")
        results["unpark_accepted"] = None

    print("\nNOTE: FindHome is intentionally NOT called automatically in this test --")
    print("this reproduces what NINA does on first connect + GoTo, not a manual home routine.")

    time.sleep(1)

    print("\n--- Choosing a target ---")
    if results.get("canslewaltazasync"):
        print("CanSlewAltAzAsync=True is available, but this diagnostic uses RA/Dec via")
        print("SlewToCoordinatesAsync for consistency with what NINA sends by default.")
    print(f"Current telescope position: RA={before.get('RightAscension')}h  "
          f"Dec={before.get('Declination')}deg   Alt={before.get('Altitude')}  "
          f"Az={before.get('Azimuth')}")
    if ra is None or dec is None:
        ra_str = input("Target RA in decimal hours (blank = current RA + 1h): ").strip()
        dec_str = input("Target Dec in decimal degrees (blank = same as current): ").strip()
        try:
            ra = float(ra_str) if ra_str else (float(before.get("RightAscension") or 0) + 1.0) % 24
            dec = float(dec_str) if dec_str else float(before.get("Declination") or 0)
        except (TypeError, ValueError):
            print("Could not parse target -- aborting test.")
            return results
    else:
        print(f"Using provided target: RA={ra} Dec={dec}")
    if not (-90 <= dec <= 90):
        print(f"Dec {dec} out of range -- aborting.")
        return results
    results["target"] = {"ra": ra, "dec": dec}

    print("\n=== BEFORE FIRST SLEW ===\n")
    print(_fmt_snapshot(_telescope_snapshot(telescope)))

    print(f"\nSending SlewToCoordinatesAsync(RA={ra}, Dec={dec})...")
    status, body, elapsed = telescope.raw_put(
        "slewtocoordinatesasync", RightAscension=ra, Declination=dec
    )
    print(f"HTTP {status}  ({elapsed:.1f}ms)")
    print(body)
    results["slew_response"] = body
    results["slew_accepted"] = body.get("ErrorNumber", -1) == 0

    if not results["slew_accepted"]:
        print("\nSlew was rejected -- see ErrorNumber/ErrorMessage above. Stopping test.")
        return results

    print("\n--- Polling ---")
    t0 = time.time()
    while time.time() - t0 < 60:
        t = time.time() - t0
        snap = _telescope_snapshot(telescope)
        print(f"t={t:5.1f}  Slewing={snap.get('Slewing')}  AtPark={snap.get('AtPark')}  "
              f"Alt={snap.get('Altitude')}  Az={snap.get('Azimuth')}  "
              f"RA={snap.get('RightAscension')}  Dec={snap.get('Declination')}")
        if snap.get("Slewing") is False:
            break
        time.sleep(1)
    else:
        print("Timed out after 60s still Slewing=True.")

    after = _telescope_snapshot(telescope)
    results["after"] = after
    results["coordinate_movement_seen"] = _coords_changed(before, after)

    print("\n--- Result ---")
    print(_fmt_snapshot(after))
    print("\nCAUTION: RightAscension/Declination/Altitude/Azimuth are self-reported by the")
    print("driver. On some drivers these update the instant a slew is ACCEPTED -- before")
    print("any motor turns -- so a coordinate change here is NOT independent proof of")
    print("physical motion. Please watch the physical unit and confirm with your own eyes.")

    if not results["coordinate_movement_seen"]:
        print("""
WARNING:
The Alpaca slew command was accepted but no telescope movement
was detected.

Possible causes:
- old Seestar Alpaca version
- Seestar startup-state issue
- invalid initialization/site/time state
- firmware bug
""")

    return results


# ---------------------------------------------------------------------------
# Manual Axis Movement Test (MoveAxis) -- explicitly user-triggered, since it
# commands real (if brief) motor motion.
# ---------------------------------------------------------------------------

def run_moveaxis_test(telescope):
    print("=== MANUAL AXIS MOVEMENT TEST ===\n")
    results = {}

    for axis, axis_name in ((0, "Primary (RA/Az)"), (1, "Secondary (Dec/Alt)")):
        can = report_action(telescope, f"CanMoveAxis({axis})", lambda a=axis: telescope.can_move_axis(a))
        results[f"canmoveaxis{axis}"] = can
        if not can:
            print(f"Axis {axis} ({axis_name}): CanMoveAxis=False -- skipping.\n")
            continue

        rates = report_action(telescope, f"AxisRates({axis})", lambda a=axis: telescope.axis_rates(a))
        results[f"axisrates{axis}"] = rates
        if not rates:
            print(f"Axis {axis} ({axis_name}): no axis rates reported -- skipping.\n")
            continue
        print(f"Axis {axis} ({axis_name}) supported rates (ranges, units are driver-defined -- "
              f"commonly deg/sec): {rates}")

        # AxisRates gives continuous [Minimum, Maximum] range(s); Minimum=0 is
        # normal (means "down to stationary"), not "no rate available". Suggest
        # a gentle default within the highest range, but let the user confirm
        # or override rather than silently picking a value for real hardware.
        try:
            highest_max = max(float(r["Maximum"]) for r in rates)
        except (KeyError, TypeError, ValueError):
            highest_max = 0.0
        if highest_max <= 0:
            print("AxisRates reports no usable positive rate -- skipping.\n")
            continue
        suggested_rate = min(1.0, highest_max)

        rate_str = input(f"Rate to use for axis {axis} (0-{highest_max}) "
                          f"[default {suggested_rate}, blank to skip]: ").strip()
        if not rate_str:
            print("Skipped by user.\n")
            continue
        try:
            safe_rate = float(rate_str)
        except ValueError:
            print("Invalid rate -- skipping.\n")
            continue
        if not (0 < safe_rate <= highest_max):
            print(f"Rate must be > 0 and <= {highest_max} -- skipping.\n")
            continue

        go = input(f"About to pulse axis {axis} ({axis_name}) at rate {safe_rate} for 0.5s. "
                   f"Watch the physical unit now. Proceed? (y/n): ").strip().lower()
        if go != "y":
            print("Skipped by user.\n")
            continue

        before = _telescope_snapshot(telescope)
        print(f"MoveAxis(Axis={axis}, Rate={safe_rate})...")
        try_action(telescope, "MoveAxis start", lambda a=axis, r=safe_rate: telescope.move_axis(a, r))
        time.sleep(0.5)
        try_action(telescope, "MoveAxis stop", lambda a=axis: telescope.move_axis(a, 0))
        after = _telescope_snapshot(telescope)

        moved = _coords_changed(before, after, epsilon=0.02)
        results[f"axis{axis}_reported_movement"] = moved
        print(f"Before: {before}")
        print(f"After:  {after}")
        print(f"Reported coordinate change: {moved}")

        seen = input("Did you visually see the arm/mount physically move? (y/n/unsure): ").strip().lower()
        results[f"axis{axis}_visual_confirm"] = seen
        print()

    return results


# ---------------------------------------------------------------------------
# Final summary
# ---------------------------------------------------------------------------

def _yn(v):
    if v is True:
        return "YES"
    if v is False:
        return "NO"
    return "UNKNOWN" if v is None else str(v)


def print_diagnostic_summary(software_info, slew_results, moveaxis_results):
    software_info = software_info or {}
    slew_results = slew_results or {}
    moveaxis_results = moveaxis_results or {}

    print("=" * 40)
    print("SEESTAR ARM / MOVEMENT DIAGNOSTIC")
    print("=" * 40 + "\n")

    print("Alpaca reachable:            YES")
    print(f"Telescope connected:         {_yn(slew_results.get('connected'))}")
    print(f"CanUnpark:                   {_yn(slew_results.get('canunpark'))}")
    print(f"CanFindHome:                 {_yn(slew_results.get('canfindhome'))}")
    print(f"CanSlewAsync:                {_yn(slew_results.get('canslewasync'))}")
    print(f"CanSlewAltAzAsync:           {_yn(slew_results.get('canslewaltazasync'))}")
    print(f"CanMoveAxis Primary:         {_yn(moveaxis_results.get('canmoveaxis0'))}")
    print(f"CanMoveAxis Secondary:       {_yn(moveaxis_results.get('canmoveaxis1'))}")
    print()
    print(f"Unpark accepted:             {_yn(slew_results.get('unpark_accepted'))}")
    print(f"First slew accepted:         {_yn(slew_results.get('slew_accepted'))}")
    print(f"Coordinate movement seen:    {_yn(slew_results.get('coordinate_movement_seen'))}  "
          f"(self-reported by driver -- confirm visually)")

    axis_moves = [v for k, v in moveaxis_results.items() if k.endswith("_reported_movement")]
    axis_visual = [v for k, v in moveaxis_results.items() if k.endswith("_visual_confirm")]
    if axis_moves:
        print(f"Manual MoveAxis movement:    {_yn(any(axis_moves))}  "
              f"(visual confirmations: {axis_visual})")
    else:
        print("Manual MoveAxis movement:    NOT TESTED")

    print("\nLIKELY INTERPRETATION:\n")

    if software_info.get("is_old_version"):
        print(f"This Seestar reports version {software_info.get('checked_version')}, older than")
        print("1.1.3-1. ZWO fixed a known issue in 1.1.3-1 where automatic GoTo startup")
        print("would not raise the arm unless the Seestar app had connected first or the")
        print("arm had been moved manually. Update firmware/app before further Python-side")
        print("debugging.")
    elif slew_results.get("slew_accepted") and slew_results.get("coordinate_movement_seen") is False:
        print("The Alpaca interface is operational but the automatic GoTo startup is not")
        print("causing any self-reported telescope movement. This matches the known ZWO")
        print("startup-state issue -- if version info above looks current, this may need")
        print("to be reported to ZWO directly.")
    elif slew_results.get("coordinate_movement_seen"):
        print("Native Alpaca automatic arm startup reports coordinate movement after the")
        print("slew -- please confirm this matches what you physically observed. If yes,")
        print("native Alpaca GoTo startup is working.")
    else:
        print("Insufficient data to draw a conclusion -- run the First Slew Test (menu 18).")
