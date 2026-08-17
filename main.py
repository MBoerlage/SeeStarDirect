"""
SEESTAR S30 PRO -- NATIVE ZWO ALPACA CONTROLLER

Talks directly to the S30 Pro's own native ASCOM Alpaca server over HTTP.
No bridge, no proxy, no port-4700 proprietary protocol, no COM driver, no
NINA. NINA (or this program) are just Alpaca clients talking to the same
server.

KNOWN FIRMWARE QUIRK (confirmed live, 2026-08-17, Alpaca/driver version
1.1.1-1 -- ZWO says fixed in 1.1.3-1): after a power cycle, the arm/motor
subsystem does not respond to native Alpaca commands (Unpark/FindHome/
MoveAxis/SlewToCoordinatesAsync all report success and update self-
consistent coordinates, but nothing physically moves) until the official
Seestar mobile app has connected once. Once the app has connected and the
arm has risen, it can be closed -- native Alpaca then drives the telescope
normally for the rest of that power cycle. This is a one-time-per-power-
cycle step, not an ongoing dependency; see diagnostics.py / README.md.

Run: python main.py   (or double-click run.bat)
"""

import logging
import os
import time
from datetime import datetime

import numpy as np

import centering
import daylight_capability_test
import diagnostics
import imaging
import platesolver
from alpaca import AlpacaError, AlpacaServer, discover_servers
from config import load_config, save_config

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(SCRIPT_DIR, "logs")

# Convenience target catalog (J2000). Deliberately does NOT include Solar
# System objects like Jupiter -- their RA/Dec change day to day, and hard
# -coding a snapshot would silently go stale. Enter those manually with
# current coordinates from a planetarium app/ephemeris, or ask for a
# skyfield-based lookup to be added later.
CATALOG = {
    "polaris": (2.5303, 89.2641),
    "m31": (0.7123, 41.2692),   # Andromeda Galaxy
    "m42": (5.5891, -5.3911),   # Orion Nebula
    "m45": (3.7913, 24.1167),   # Pleiades
}


def setup_logging():
    os.makedirs(LOGS_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(LOGS_DIR, f"seestar_alpaca_{ts}.log")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    root.addHandler(handler)
    return path


class AppState:
    def __init__(self, cfg):
        self.cfg = cfg
        self.server: AlpacaServer | None = None
        self.devices = {}       # (device_type, device_number) -> device object
        self.last_exposure = None  # (camera_device, numpy array, sensor_type)
        self.software_info = {}
        self.slew_test_results = {}
        self.moveaxis_test_results = {}

    def by_type(self, device_type):
        return [d for (t, _), d in self.devices.items() if t == device_type]

    def pick(self, device_type, prompt_label):
        candidates = self.by_type(device_type)
        if not candidates:
            print(f"No {device_type} device known -- run discovery first (menu 1/3).")
            return None
        if len(candidates) == 1:
            return candidates[0]
        print(f"Multiple {device_type} devices:")
        for d in candidates:
            print(f"  {d.device_number}: {d.display_name()}")
        choice = input(f"Pick {prompt_label} device number: ").strip()
        for d in candidates:
            if str(d.device_number) == choice:
                return d
        print("Invalid selection.")
        return None


# ---------------------------------------------------------------------------
# Menu 1-4: discovery / server / devices / capabilities
# ---------------------------------------------------------------------------

def do_discover(state):
    print("Searching for Alpaca devices...\n")
    timeout = state.cfg.get("discovery_timeout", 3)
    found = discover_servers(timeout=timeout)

    if not found and state.cfg.get("preferred_server_ip"):
        ip = state.cfg["preferred_server_ip"]
        port = state.cfg.get("preferred_server_port") or 32323
        print(f"Discovery found nothing; falling back to configured server {ip}:{port}")
        found = [{"ip": ip, "port": port}]

    if not found:
        print("No Alpaca discovery responses received.")
        print("-> Confirm the Seestar is powered on, in Station Mode, and on the same LAN.")
        print("-> If your network blocks UDP broadcast, set preferred_server_ip/"
              "preferred_server_port in config.json as a manual fallback.")
        return

    server_info = found[0]
    if len(found) > 1:
        print(f"Found {len(found)} Alpaca servers, using the first: {server_info}")

    server = AlpacaServer(server_info["ip"], server_info["port"], state.cfg["client_id"])
    try:
        server.query_management()
    except Exception as e:
        print(f"Found a discovery response but the management API failed: {e}")
        return

    state.server = server
    state.devices = {}
    for info in server.configured_devices:
        dev = server.make_device(info)
        state.devices[(dev.device_type, dev.device_number)] = dev

    desc = server.description
    print("Found Alpaca server:\n")
    print(f"IP:           {server.ip}")
    print(f"Port:         {server.port}")
    print(f"Server:       {desc.get('ServerName')}")
    print(f"Manufacturer: {desc.get('Manufacturer')}")
    print(f"Version:      {desc.get('ManufacturerVersion')}")
    print("\nConfigured devices:\n")
    for info in server.configured_devices:
        print(f"{info['DeviceNumber']:<3}{info['DeviceType']:<14}{info['DeviceName']}")

    telescopes = state.by_type("telescope")
    if telescopes:
        print()
        state.software_info = diagnostics.print_software_info(server, telescopes[0])
        print("\nREMINDER: on this firmware, the arm/motors will not respond to Alpaca")
        print("commands after a fresh power-cycle until the official Seestar app has")
        print("connected once (confirmed workaround for the known 1.1.1-1 bug). If you")
        print("haven't opened the app since the last power-on, do that first, then close")
        print("it -- native Alpaca control (menu 12/18/etc.) should then work normally.")


def do_show_server_info(state):
    if not state.server:
        print("No server discovered yet -- use option 1.")
        return
    print(f"Base URL: {state.server.base_url}")
    print(f"API versions: {state.server.api_versions}")
    print(f"Description: {state.server.description}")


def do_enumerate_devices(state):
    if not state.devices:
        print("No devices known yet -- use option 1.")
        return
    for (dtype, dnum), dev in sorted(state.devices.items()):
        try:
            connected = dev.connected
        except AlpacaError as e:
            connected = f"ERROR {e}"
        print(f"{dtype.capitalize():<12} {dnum:<3} {'Connected' if connected is True else connected}"
              f"  {dev.display_name()}")


def do_show_capabilities(state):
    if not state.devices:
        print("No devices known yet -- use option 1.")
        return
    diagnostics.print_capability_report(list(state.devices.values()))


# ---------------------------------------------------------------------------
# Telescope menu
# ---------------------------------------------------------------------------

def telescope_connect(state):
    t = state.pick("telescope", "telescope")
    if not t:
        return
    diagnostics.report_action(t, "Connect", lambda: setattr(t, "connected", True))


def telescope_show_state(state):
    t = state.pick("telescope", "telescope")
    if not t:
        return
    for label, prop in [
        ("Connected", "connected"), ("AtPark", "atpark"), ("Slewing", "slewing"),
        ("Tracking", "tracking"), ("RightAscension", "rightascension"),
        ("Declination", "declination"), ("Altitude", "altitude"), ("Azimuth", "azimuth"),
    ]:
        try:
            val = getattr(t, prop)
        except AlpacaError as e:
            val = f"ERROR ({e.error_number}) {e.error_message}"
        print(f"  {label:<16}{val}")


def telescope_initialize(state):
    t = state.pick("telescope", "telescope")
    if not t:
        return
    diagnostics.initialize_telescope(t)


def telescope_find_home(state):
    t = state.pick("telescope", "telescope")
    if not t:
        return
    if not diagnostics.report_action(t, "CanFindHome", lambda: t.canfindhome):
        print("Device does not report CanFindHome=True -- not sending FindHome.")
        return
    diagnostics.report_action(t, "FindHome", t.find_home)


def telescope_slew(state):
    t = state.pick("telescope", "telescope")
    if not t:
        return
    if not t.connected:
        print("Telescope is not connected.")
        return
    if not diagnostics.report_action(t, "CanSlewAsync", lambda: t.canslewasync):
        print("Device does not report CanSlewAsync=True -- aborting.")
        return

    print("Known targets: " + ", ".join(sorted(CATALOG.keys())))
    raw = input("Target name, or 'RA,Dec' in decimal hours,degrees: ").strip()
    if not raw:
        print("Cancelled.")
        return
    key = raw.lower()
    if key in CATALOG:
        ra, dec = CATALOG[key]
    else:
        try:
            ra_s, dec_s = raw.split(",")
            ra, dec = float(ra_s), float(dec_s)
        except ValueError:
            print("Could not parse. Use a catalog name or 'RA,Dec', e.g. '5.59,-5.39'.")
            return

    print(f"Slewing to RA={ra:.4f}h Dec={dec:.4f}deg ...")
    result = diagnostics.report_action(
        t, "SlewToCoordinatesAsync", lambda: t.slew_to_coordinates_async(ra, dec)
    )
    if result is None and not isinstance(result, dict):
        pass  # report_action already printed FAILED/OK

    print("Polling until slew completes (Ctrl+C to stop watching) ...")
    try:
        for _ in range(180):
            time.sleep(1)
            state_vals = {}
            for prop in ("rightascension", "declination", "altitude", "azimuth",
                         "atpark", "tracking", "slewing"):
                try:
                    state_vals[prop] = t.get(prop)
                except AlpacaError:
                    state_vals[prop] = "?"
            print(f"  ra={state_vals['rightascension']:.4f} dec={state_vals['declination']:.4f} "
                  f"alt={state_vals['altitude']:.2f} az={state_vals['azimuth']:.2f} "
                  f"tracking={state_vals['tracking']} slewing={state_vals['slewing']}")
            if not state_vals["slewing"]:
                print("Slew complete.")
                return
        print("Still slewing after 3 minutes -- giving up watching (it may still finish).")
    except KeyboardInterrupt:
        print("\nStopped watching (slew may still be in progress).")


def telescope_abort_slew(state):
    t = state.pick("telescope", "telescope")
    if not t:
        return
    diagnostics.report_action(t, "AbortSlew", t.abort_slew)


def telescope_set_tracking(state):
    t = state.pick("telescope", "telescope")
    if not t:
        return
    if not diagnostics.report_action(t, "CanSetTracking", lambda: t.cansettracking):
        print("Device does not report CanSetTracking=True -- aborting.")
        return
    try:
        current = t.tracking
    except AlpacaError as e:
        print(f"Could not read current Tracking: {e}")
        return
    val = input(f"Tracking is currently {current}. Set to (on/off): ").strip().lower()
    if val not in ("on", "off"):
        print("Enter 'on' or 'off'.")
        return
    diagnostics.report_action(t, "Set Tracking", lambda: setattr(t, "tracking", val == "on"))
    time.sleep(1)
    try:
        print(f"Tracking is now {t.tracking}")
    except AlpacaError as e:
        print(f"Could not confirm Tracking: {e}")


def telescope_park(state):
    t = state.pick("telescope", "telescope")
    if not t:
        return
    if not diagnostics.report_action(t, "CanPark", lambda: t.canpark):
        print("Device does not report CanPark=True -- not sending Park.")
        return
    diagnostics.report_action(t, "Park", t.park)


def telescope_software_info(state):
    if not state.server:
        print("No server discovered yet -- use option 1.")
        return
    t = state.pick("telescope", "telescope")
    if not t:
        return
    state.software_info = diagnostics.print_software_info(state.server, t)


def telescope_first_slew_test(state):
    t = state.pick("telescope", "telescope")
    if not t:
        return
    state.slew_test_results = diagnostics.run_first_slew_test(t)


def telescope_moveaxis_test(state):
    t = state.pick("telescope", "telescope")
    if not t:
        return
    if not t.connected:
        print("Telescope is not connected (use option 10).")
        return
    state.moveaxis_test_results = diagnostics.run_moveaxis_test(t)


# ---------------------------------------------------------------------------
# Camera menu
# ---------------------------------------------------------------------------

def camera_list(state):
    cams = state.by_type("camera")
    if not cams:
        print("No cameras known yet -- use option 1.")
        return
    for c in cams:
        try:
            x, y = c.cameraxsize, c.cameraysize
        except AlpacaError:
            x = y = "?"
        print(f"Camera {c.device_number}: {c.display_name()}  {x}x{y}")


def camera_connect(state):
    c = state.pick("camera", "camera")
    if not c:
        return
    diagnostics.report_action(c, "Connect", lambda: setattr(c, "connected", True))


def camera_take_exposure(state):
    c = state.pick("camera", "camera")
    if not c:
        return
    if not c.connected:
        print("Camera is not connected (use option 21).")
        return

    try:
        exp_min, exp_max = c.exposuremin, c.exposuremax
    except AlpacaError as e:
        print(f"Could not read exposure limits: {e}")
        return
    duration_str = input(f"Exposure seconds [{exp_min:.3g}-{exp_max:.3g}, default 2]: ").strip()
    try:
        duration = float(duration_str) if duration_str else 2.0
    except ValueError:
        print("Invalid duration.")
        return

    try:
        sensor_type = c.sensortype
    except AlpacaError:
        sensor_type = None

    print(f"Starting {duration}s exposure on {c.label()} ...")
    try:
        array = imaging.take_exposure(c, duration, light=True)
    except AlpacaError as e:
        print(f"[{c.label()}] StartExposure FAILED")
        print(f"  Alpaca ErrorNumber: {e.error_number}")
        print(f"  ErrorMessage: {e.error_message}")
        return
    except imaging.ExposureTimeout as e:
        print(f"[{c.label()}] {e}")
        return

    arr = np.asarray(array)
    print(f"[{c.label()}] Exposure OK -- array shape {arr.shape}, dtype {arr.dtype}, "
          f"min={arr.min()} max={arr.max()}")
    state.last_exposure = (c, arr, sensor_type)
    print("Use option 23 to save this exposure as FITS + PNG.")


def camera_save_last(state):
    """Returns {"fits": path, "png": path or None, "dir": out_dir} on success,
    None if there was nothing to save. (main.py's console menu ignores the
    return value; gui.py uses it to show the exact path and preview.)"""
    if not state.last_exposure:
        print("No exposure taken yet -- use option 22 first.")
        return None
    c, arr, sensor_type = state.last_exposure
    out_dir = os.path.join(SCRIPT_DIR, state.cfg.get("output_directory", "images"))
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = f"seestar_cam{c.device_number}_{ts}"

    fits_path = os.path.join(out_dir, base + ".fits")
    imaging.write_fits(fits_path, arr, header_items=[
        ("INSTRUME", c.display_name(), "camera device name"),
        ("SENSTYPE", sensor_type if sensor_type is not None else -1, "ASCOM SensorType enum"),
    ])
    print(f"Saved FITS (raw pixel values): {fits_path}")

    png_path = None
    try:
        png_path = os.path.join(out_dir, base + "_preview.png")
        imaging.save_preview_png(png_path, arr, sensor_type=sensor_type)
        print(f"Saved PNG preview (quick-look, lossy): {png_path}")
    except Exception as e:
        png_path = None
        print(f"PNG preview failed (FITS above is still valid): {e}")

    return {"fits": fits_path, "png": png_path, "dir": out_dir}


# ---------------------------------------------------------------------------
# Slew & Center (native Alpaca + ASTAP) -- same backend as gui.py, no
# Tkinter here. See centering.py for the actual workflow.
# ---------------------------------------------------------------------------

def telescope_slew_and_center(state):
    t = state.pick("telescope", "telescope")
    if not t:
        return
    cams = state.by_type("camera")
    if not cams:
        print("No cameras known -- run Discover first.")
        return

    print("Available cameras:")
    for c in cams:
        print(f"  {c.device_number}: {c.display_name()}")
    default_cam = state.cfg.get("centering_camera", 0)
    cam_str = input(f"Camera number [{default_cam}]: ").strip()
    try:
        camera_number = int(cam_str) if cam_str else default_cam
    except ValueError:
        print("Invalid camera number.")
        return

    print("\nKnown targets: " + ", ".join(sorted(CATALOG.keys())))
    raw = input("Target name, or 'RA,Dec' in decimal hours,degrees: ").strip()
    if not raw:
        print("Cancelled.")
        return
    key = raw.lower()
    if key in CATALOG:
        ra, dec = CATALOG[key]
    else:
        try:
            ra_s, dec_s = raw.split(",")
            ra, dec = float(ra_s), float(dec_s)
        except ValueError:
            print("Could not parse target. Use a catalog name or 'RA,Dec', e.g. '5.59,-5.39'.")
            return

    def ask_float(prompt, default):
        s = input(f"{prompt} [{default}]: ").strip()
        if not s:
            return default
        try:
            return float(s)
        except ValueError:
            print("Invalid number, using default.")
            return default

    exposure = ask_float("Exposure seconds", state.cfg.get("centering_exposure_seconds", 2.0))
    tolerance = ask_float("Tolerance (arcmin)", state.cfg.get("centering_tolerance_arcmin", 5.0))
    iterations_default = state.cfg.get("centering_max_iterations", 3)
    iterations_s = input(f"Max iterations [{iterations_default}]: ").strip()
    try:
        iterations = int(iterations_s) if iterations_s else iterations_default
    except ValueError:
        print("Invalid number, using default.")
        iterations = iterations_default

    astap_path = state.cfg.get("astap_path", "")
    if not platesolver.is_valid_astap_path(astap_path):
        detected = platesolver.find_astap()
        if detected:
            use = input(f"ASTAP not configured. Detected at {detected} -- "
                        f"use it for this run only? [y/N]: ").strip().lower()
            if use == "y":
                astap_path = detected
    if not platesolver.is_valid_astap_path(astap_path):
        print("No valid ASTAP executable configured -- set 'astap_path' in config.json "
              "(or the GUI's Settings tab) first.")
        return

    out_dir = os.path.join(SCRIPT_DIR, state.cfg.get("output_directory", "images"))
    result = centering.slew_and_center(
        state, ra, dec, camera_number, exposure, tolerance, iterations,
        astap_path, plate_solve_timeout=state.cfg.get("plate_solve_timeout", 60),
        min_altitude_deg=state.cfg.get("minimum_target_altitude_deg", 20.0),
        sun_exclusion_deg=state.cfg.get("sun_exclusion_deg", 30.0),
        output_dir=out_dir,
    )
    print(f"\nSlew & Center finished: {'SUCCESS' if result['success'] else 'DID NOT COMPLETE'}")
    print(result["message"])


# ---------------------------------------------------------------------------
# Focuser menu
# ---------------------------------------------------------------------------

def focuser_show(state):
    focusers = state.by_type("focuser")
    if not focusers:
        print("No focusers known yet -- use option 1.")
        return
    for f in focusers:
        print(f"\n--- Focuser {f.device_number}: {f.display_name()} ---")
        for label, prop in [("Position", "position"), ("MaxStep", "maxstep"),
                             ("MaxIncrement", "maxincrement"), ("IsMoving", "ismoving"),
                             ("Absolute", "absolute"), ("Temperature", "temperature"),
                             ("TempComp", "tempcomp"), ("TempCompAvailable", "tempcompavailable")]:
            try:
                print(f"  {label:<18}{getattr(f, prop)}")
            except AlpacaError as e:
                print(f"  {label:<18}ERROR ({e.error_number}) {e.error_message}")


def focuser_move(state):
    f = state.pick("focuser", "focuser")
    if not f:
        return
    if not f.connected:
        diagnostics.report_action(f, "Connect", lambda: setattr(f, "connected", True))
    try:
        maxstep = f.maxstep
    except AlpacaError as e:
        print(f"Could not read MaxStep: {e}")
        return
    pos_str = input(f"Absolute position [0-{maxstep}]: ").strip()
    try:
        pos = int(pos_str)
    except ValueError:
        print("Invalid position.")
        return
    diagnostics.report_action(f, "Move", lambda: f.move(pos))


def focuser_halt(state):
    f = state.pick("focuser", "focuser")
    if not f:
        return
    diagnostics.report_action(f, "Halt", f.halt)


# ---------------------------------------------------------------------------
# Filter wheel menu
# ---------------------------------------------------------------------------

def filterwheel_show(state):
    fw = state.pick("filterwheel", "filter wheel")
    if not fw:
        return
    if not fw.connected:
        diagnostics.report_action(fw, "Connect", lambda: setattr(fw, "connected", True))
    try:
        names = fw.names
        pos = fw.position
    except AlpacaError as e:
        print(f"Could not read filter wheel: {e}")
        return
    print(f"Current position: {pos}\n")
    for i, name in enumerate(names):
        marker = " <-- current" if i == pos else ""
        print(f"{i}  {name}{marker}")


def filterwheel_select(state):
    fw = state.pick("filterwheel", "filter wheel")
    if not fw:
        return
    try:
        names = fw.names
    except AlpacaError as e:
        print(f"Could not read filter names: {e}")
        return
    for i, name in enumerate(names):
        print(f"{i}  {name}")
    choice = input("Select filter by number or name: ").strip()
    idx = None
    if choice.isdigit():
        idx = int(choice)
    else:
        for i, name in enumerate(names):
            if name.lower() == choice.lower():
                idx = i
                break
    if idx is None or not (0 <= idx < len(names)):
        print("Invalid selection.")
        return
    diagnostics.report_action(fw, "SetPosition", lambda: setattr(fw, "position", idx))


# ---------------------------------------------------------------------------
# Switch menu
# ---------------------------------------------------------------------------

def switch_show(state):
    sw = state.pick("switch", "switch")
    if not sw:
        return
    if not sw.connected:
        diagnostics.report_action(sw, "Connect", lambda: setattr(sw, "connected", True))
    diagnostics._print_switch_detail(sw)


def switch_set(state):
    sw = state.pick("switch", "switch")
    if not sw:
        return
    try:
        n = sw.maxswitch
    except AlpacaError as e:
        print(f"Could not read MaxSwitch: {e}")
        return
    idx_str = input(f"Switch id [0-{n - 1}]: ").strip()
    try:
        idx = int(idx_str)
    except ValueError:
        print("Invalid id.")
        return
    try:
        can_write = sw.can_write(idx)
    except AlpacaError as e:
        print(f"Could not read CanWrite: {e}")
        return
    if not can_write:
        print(f"Switch {idx} is read-only (CanWrite=False) -- not sending anything.")
        return
    val = input("New value (0/1 or true/false): ").strip().lower()
    state_bool = val in ("1", "true", "on", "yes")
    diagnostics.report_action(sw, "SetSwitch", lambda: sw.set_switch(idx, state_bool))


# ---------------------------------------------------------------------------
# Diagnostics menu
# ---------------------------------------------------------------------------

def show_supported_actions(state):
    if not state.devices:
        print("No devices known yet -- use option 1.")
        return
    for dev in state.devices.values():
        try:
            actions = dev.supportedactions
        except AlpacaError as e:
            actions = f"ERROR ({e.error_number}) {e.error_message}"
        print(f"{dev.label():<14} {dev.display_name():<40} {actions}")


def dump_device_info(state):
    if not state.devices:
        print("No devices known yet -- use option 1.")
        return
    for dev in state.devices.values():
        print(f"{dev.label()}: {dev.raw_info}")


def _parse_kv(s):
    result = {}
    if not s.strip():
        return result
    for pair in s.split(","):
        if "=" not in pair:
            continue
        k, v = pair.split("=", 1)
        result[k.strip()] = v.strip()
    return result


def show_diagnostic_summary(state):
    diagnostics.print_diagnostic_summary(
        state.software_info, state.slew_test_results, state.moveaxis_test_results
    )


def raw_get(state):
    if not state.server:
        print("No server discovered yet -- use option 1.")
        return
    dtype = input("Device type (telescope/camera/focuser/filterwheel/switch): ").strip().lower()
    dnum = input("Device number [0]: ").strip() or "0"
    member = input("Member (e.g. atpark): ").strip().lower()
    params = _parse_kv(input("Extra params as key=value,key2=value2 [none]: "))

    dev = state.devices.get((dtype, int(dnum)))
    if dev is None:
        print(f"No such device known ({dtype} {dnum}) -- check option 3.")
        return
    status, body, elapsed = dev.raw_get(member, **params)
    print(f"GET {dev._url(member)}")
    print(f"HTTP {status}  ({elapsed:.1f}ms)")
    print(body)


def raw_put(state):
    if not state.server:
        print("No server discovered yet -- use option 1.")
        return
    dtype = input("Device type (telescope/camera/focuser/filterwheel/switch): ").strip().lower()
    dnum = input("Device number [0]: ").strip() or "0"
    member = input("Member (e.g. connected): ").strip().lower()
    data = _parse_kv(input("PUT data as key=value,key2=value2 [none]: "))

    dev = state.devices.get((dtype, int(dnum)))
    if dev is None:
        print(f"No such device known ({dtype} {dnum}) -- check option 3.")
        return
    status, body, elapsed = dev.raw_put(member, **data)
    print(f"PUT {dev._url(member)}  data={data}")
    print(f"HTTP {status}  ({elapsed:.1f}ms)")
    print(body)


# ---------------------------------------------------------------------------
# Menu
# ---------------------------------------------------------------------------

MENU_TEXT = """
=====================================================
 SEESTAR S30 PRO -- NATIVE ZWO ALPACA CONTROLLER
=====================================================

1. Discover Seestar
2. Show server information
3. Enumerate devices
4. Show all capabilities

TELESCOPE
10. Connect
11. Show state
12. Initialize / open arm
13. Find home
14. Slew to coordinates
15. Abort slew
16. Park
17. Set tracking on/off
18. Auto Start / First Slew Test (reproduces NINA's Connect+GoTo)
19. Manual Axis Movement Test (MoveAxis)

CAMERA
20. List cameras
21. Connect camera
22. Take test exposure
23. Save last exposure (FITS + PNG)

FOCUSER
30. Show focusers
31. Move focuser
32. Halt focuser

FILTER WHEEL
40. Show filters
41. Select filter

SWITCH
50. Show switches
51. Set switch

SLEW & CENTER
60. Slew & Center (native Alpaca SlewToCoordinatesAsync + ASTAP plate
    solve + Sync/corrected reslew loop -- see Settings/config.json for
    defaults: astap_path, centering_*, minimum_target_altitude_deg,
    sun_exclusion_deg)

DAYLIGHT TEST
80. Daylight Alpaca / NINA Compatibility Test (safe: no exposure, no slew,
    no arm movement unless you explicitly confirm)

DIAGNOSTICS
90. Show SupportedActions
91. Dump Alpaca device information
92. Raw Alpaca GET
93. Raw Alpaca PUT
94. Show software/version info
95. Show arm/movement diagnostic summary

0. Exit
"""

HANDLERS = {
    "1": do_discover,
    "2": do_show_server_info,
    "3": do_enumerate_devices,
    "4": do_show_capabilities,
    "10": telescope_connect,
    "11": telescope_show_state,
    "12": telescope_initialize,
    "13": telescope_find_home,
    "14": telescope_slew,
    "15": telescope_abort_slew,
    "16": telescope_park,
    "17": telescope_set_tracking,
    "18": telescope_first_slew_test,
    "19": telescope_moveaxis_test,
    "20": camera_list,
    "21": camera_connect,
    "22": camera_take_exposure,
    "23": camera_save_last,
    "30": focuser_show,
    "31": focuser_move,
    "32": focuser_halt,
    "40": filterwheel_show,
    "41": filterwheel_select,
    "50": switch_show,
    "51": switch_set,
    "60": telescope_slew_and_center,
    "80": daylight_capability_test.run_daylight_capability_test,
    "90": show_supported_actions,
    "91": dump_device_info,
    "92": raw_get,
    "93": raw_put,
    "94": telescope_software_info,
    "95": show_diagnostic_summary,
}


def main():
    log_path = setup_logging()
    print(f"Logging Alpaca traffic (DEBUG) to {log_path}\n")

    cfg = load_config()
    state = AppState(cfg)

    while True:
        print(MENU_TEXT)
        if state.server:
            print(f"Server: {state.server.ip}:{state.server.port}  "
                  f"({len(state.devices)} device(s) known)\n")
        choice = input("Choose an option: ").strip()

        if choice == "0":
            print("Goodbye.")
            return

        handler = HANDLERS.get(choice)
        if not handler:
            print("Invalid option, try again.")
            continue

        try:
            handler(state)
        except KeyboardInterrupt:
            print("\nInterrupted.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
