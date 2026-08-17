"""
Simple ASCOM Alpaca control console for the Seestar S30 Pro.

IMPORTANT: the Seestar does not speak Alpaca natively. This talks to the
"seestar_alp" bridge (https://github.com/smart-underworld/seestar_alp),
which must be running -- normally on this same laptop -- and which in turn
talks to the Seestar's native protocol on port 4700. Start it with
run_seestar_alp_bridge.bat before using this script. Its own web UI (handy
for watching what's happening) is at http://127.0.0.1:5432 by default.

No third-party packages required for THIS script (uses only the Python
standard library). Config (bridge IP/port) is stored next to this script in
seestar_alpaca_config.json and reloaded automatically next time you run it.

Captured images are downloaded as-is (JPG) from the Seestar into an
"images" folder next to this script.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "seestar_alpaca_config.json")
IMAGES_DIR = os.path.join(SCRIPT_DIR, "images")

DEFAULT_CONFIG = {
    # This is the seestar_alp BRIDGE address, not the Seestar's own IP.
    # The bridge normally runs on this laptop, so localhost is correct.
    "ip": "127.0.0.1",
    "port": 5555,
    "client_id": 1,
}

# A handful of well-known targets (J2000) as a convenience for "go to target".
# RA in decimal hours, Dec in decimal degrees.
CATALOG = {
    "polaris": (2.5303, 89.2641),
    "m31": (0.7123, 41.2692),   # Andromeda Galaxy
    "m42": (5.5891, -5.3911),   # Orion Nebula
    "m45": (3.7913, 24.1167),   # Pleiades
    "m13": (16.6949, 36.4603),  # Hercules Cluster
    "m51": (13.4979, 47.1952),  # Whirlpool Galaxy
    "m57": (18.8932, 33.0292),  # Ring Nebula
}

_transaction_id = 0


def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r") as f:
                cfg = json.load(f)
            merged = dict(DEFAULT_CONFIG)
            merged.update(cfg)
            return merged
        except (json.JSONDecodeError, OSError):
            print(f"[!] Could not read {CONFIG_PATH}, using defaults.")
    return dict(DEFAULT_CONFIG)


def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def base_url(cfg):
    return f"http://{cfg['ip']}:{cfg['port']}"


def next_transaction_id():
    global _transaction_id
    _transaction_id += 1
    return _transaction_id


def alpaca_get(cfg, path, params=None, timeout=5):
    params = dict(params or {})
    params.setdefault("ClientID", cfg["client_id"])
    params.setdefault("ClientTransactionID", next_transaction_id())
    url = f"{base_url(cfg)}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def alpaca_put(cfg, path, data=None, timeout=5):
    data = dict(data or {})
    data.setdefault("ClientID", cfg["client_id"])
    data.setdefault("ClientTransactionID", next_transaction_id())
    body = urllib.parse.urlencode(data).encode("utf-8")
    url = f"{base_url(cfg)}{path}"
    req = urllib.request.Request(url, data=body, method="PUT")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def alpaca_action(cfg, dtype, dnum, action_name, params, timeout=15):
    """Call seestar_alp's custom Alpaca 'Action' extension (used for
    everything the standard ASCOM Telescope interface doesn't cover:
    startup sequence, stacking, image retrieval, etc.)."""
    data = {"Action": action_name, "Parameters": json.dumps(params)}
    return alpaca_put(cfg, f"/api/v1/{dtype}/{dnum}/action", data, timeout=timeout)


def describe_error(exc):
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code} {exc.reason}"
    if isinstance(exc, urllib.error.URLError):
        return f"Connection error: {exc.reason}"
    return str(exc)


def check_alpaca_error(result, action):
    """Alpaca returns HTTP 200 even for device errors; the error is in the JSON body."""
    err_num = result.get("ErrorNumber", 0)
    if err_num:
        print(f"  [!] {action} reported an error ({err_num}): {result.get('ErrorMessage')}")
        return False
    return True


def run_action(cfg, dtype, dnum, action_name, params, timeout=15):
    """Call an Action and unwrap both the Alpaca-level and Seestar-level error envelopes."""
    try:
        result = alpaca_action(cfg, dtype, dnum, action_name, params, timeout=timeout)
    except Exception as exc:
        print(f"  [FAILED] {action_name}: {describe_error(exc)}")
        return None
    if not check_alpaca_error(result, action_name):
        return None
    value = result.get("Value")
    if isinstance(value, dict):
        if value.get("code") not in (None, 0):
            print(f"  [!] {action_name} reported failure: {value.get('result')}")
            return None
        if "error" in value:
            print(f"  [!] {action_name} reported failure: {value.get('error')}")
            return None
    return value


# ---------------------------------------------------------------------------
# Connection / discovery
# ---------------------------------------------------------------------------

def test_connection(cfg, state):
    print(f"\nContacting bridge at {base_url(cfg)} ...")
    try:
        versions = alpaca_get(cfg, "/management/apiversions")
        print(f"  Alpaca API versions supported: {versions.get('Value')}")
    except Exception as exc:
        print(f"  [FAILED] Could not reach the Alpaca management API: {describe_error(exc)}")
        print("  -> Make sure run_seestar_alp_bridge.bat is running, and that the "
              "IP/port here match its [network] settings (device/config.toml).")
        return

    try:
        desc = alpaca_get(cfg, "/management/v1/description")
        info = desc.get("Value", {})
        print(f"  Server name : {info.get('ServerName')}")
        print(f"  Manufacturer: {info.get('Manufacturer')}")
        print(f"  Version     : {info.get('ServerVersion')}")
    except Exception as exc:
        print(f"  [!] Reached the server but /management/v1/description failed: {describe_error(exc)}")

    try:
        devices = alpaca_get(cfg, "/management/v1/configureddevices")
        device_list = devices.get("Value", [])
    except Exception as exc:
        print(f"  [!] Could not list configured devices: {describe_error(exc)}")
        return

    state["devices"] = device_list
    if not device_list:
        print("  No configured devices reported.")
        return

    print(f"  Found {len(device_list)} device(s):")
    for d in device_list:
        print(f"    - {d.get('DeviceName')} (Type={d.get('DeviceType')}, Number={d.get('DeviceNumber')})")

    # seestar_alp exposes one Telescope device that also handles imaging via
    # custom Actions -- there's no separate ASCOM Camera device.
    for d in device_list:
        if d.get("DeviceType", "").lower() == "telescope" and state.get("telescope") is None:
            state["telescope"] = ("telescope", d["DeviceNumber"])

    if state.get("telescope"):
        print(f"  -> Using telescope device #{state['telescope'][1]} for all actions.")
    else:
        print("  [!] No Telescope device found among the configured devices.")


def connect_devices(cfg, state):
    device = state.get("telescope")
    if device is None:
        print("  [!] No telescope found yet -- run 'Test connection' first.")
        return
    dtype, dnum = device
    path = f"/api/v1/{dtype}/{dnum}/connected"
    try:
        alpaca_put(cfg, path, {"Connected": "true"})
        result = alpaca_get(cfg, path)
        if result.get("Value"):
            print("  [OK] Telescope connected.")
        else:
            print("  [!] Telescope did not report Connected=true.")
    except Exception as exc:
        print(f"  [FAILED] Could not connect: {describe_error(exc)}")


def ensure_connected(cfg, dtype, dnum):
    path = f"/api/v1/{dtype}/{dnum}/connected"
    try:
        result = alpaca_get(cfg, path)
        if result.get("Value"):
            return True
        alpaca_put(cfg, path, {"Connected": "true"})
        result = alpaca_get(cfg, path)
        return bool(result.get("Value"))
    except Exception as exc:
        print(f"  [!] Could not confirm connection: {describe_error(exc)}")
        return False


def require_telescope(state):
    device = state.get("telescope")
    if device is None:
        print("\nNo telescope device known yet -- run 'Test connection' first.")
    return device


# ---------------------------------------------------------------------------
# Telescope actions
# ---------------------------------------------------------------------------

def open_arm(cfg, state):
    device = require_telescope(state)
    if not device:
        return
    dtype, dnum = device
    if not ensure_connected(cfg, dtype, dnum):
        print("  [!] Telescope is not connected, aborting.")
        return

    print("\nRunning startup sequence (raises the arm, sets the clock, estimates location) ...")
    print("  This runs in the background on the bridge -- you can watch progress at "
          "http://127.0.0.1:5432")
    # Deliberately skipping auto-focus / 3-point polar align / dark frames here
    # to keep "open arm" quick. Tell me if you want those enabled too.
    value = run_action(cfg, dtype, dnum, "action_start_up_sequence", {
        "lat": 0, "lon": 0,
        "auto_focus": False, "3ppa": False, "dark_frames": False,
    })
    if value is not None:
        msg = value.get("result") if isinstance(value, dict) else value
        print(f"  [OK] {msg or 'Startup sequence started.'}")


def close_arm(cfg, state):
    device = require_telescope(state)
    if not device:
        return
    dtype, dnum = device
    print("\nSending Park command ...")
    try:
        result = alpaca_put(cfg, f"/api/v1/{dtype}/{dnum}/park")
        check_alpaca_error(result, "Park")
        print("  [OK] Park command sent.")
        print("  Note: Park is a stub in this bridge version -- it may not physically "
              "stow the mount. Tell me if you need real shutdown behavior and we'll "
              "wire up something firmware-specific.")
    except Exception as exc:
        print(f"  [FAILED] {describe_error(exc)}")


def goto_target(cfg, state):
    device = require_telescope(state)
    if not device:
        return
    dtype, dnum = device
    if not ensure_connected(cfg, dtype, dnum):
        print("  [!] Telescope is not connected, aborting.")
        return

    print("\nKnown targets: " + ", ".join(sorted(CATALOG.keys())))
    raw = input("Enter a target name above, or 'RA,Dec' in decimal hours,degrees: ").strip()
    if not raw:
        print("  Cancelled.")
        return

    key = raw.lower()
    if key in CATALOG:
        ra, dec = CATALOG[key]
    else:
        try:
            ra_str, dec_str = raw.split(",")
            ra, dec = float(ra_str), float(dec_str)
        except ValueError:
            print("  Could not parse that. Use a catalog name or 'RA,Dec', e.g. '5.59,-5.39'.")
            return

    print(f"  Slewing to RA={ra:.4f}h Dec={dec:.4f}deg ...")
    try:
        result = alpaca_put(cfg, f"/api/v1/{dtype}/{dnum}/slewtocoordinatesasync",
                             {"RightAscension": ra, "Declination": dec})
        if not check_alpaca_error(result, "SlewToCoordinatesAsync"):
            return
    except Exception as exc:
        print(f"  [FAILED] {describe_error(exc)}")
        return

    print("  Waiting for slew to complete (Ctrl+C to stop waiting) ...")
    try:
        for _ in range(120):  # up to ~2 minutes
            time.sleep(1)
            slewing = alpaca_get(cfg, f"/api/v1/{dtype}/{dnum}/slewing")
            if not slewing.get("Value"):
                print("  [OK] Slew complete.")
                return
        print("  [!] Still slewing after 2 minutes -- giving up waiting (it may still finish).")
    except KeyboardInterrupt:
        print("\n  Stopped waiting (slew may still be in progress).")


# ---------------------------------------------------------------------------
# Imaging (via seestar_alp's stacking + get_last_image actions)
# ---------------------------------------------------------------------------

def take_image(cfg, state):
    device = require_telescope(state)
    if not device:
        return
    dtype, dnum = device
    if not ensure_connected(cfg, dtype, dnum):
        print("  [!] Telescope is not connected, aborting.")
        return

    duration_str = input("Stack duration in seconds [20]: ").strip()
    try:
        duration = float(duration_str) if duration_str else 20.0
    except ValueError:
        print("  Invalid duration, cancelled.")
        return

    print("  Starting stack ...")
    if run_action(cfg, dtype, dnum, "start_stack", {"gain": 80, "restart": True}) is None:
        return

    print(f"  Stacking for {duration:.0f}s (Ctrl+C to stop early) ...")
    try:
        time.sleep(duration)
    except KeyboardInterrupt:
        print("\n  Interrupted, stopping stack early.")

    print("  Stopping stack ...")
    run_action(cfg, dtype, dnum, "method_sync",
               {"method": "iscope_stop_view", "params": {"stage": "Stack"}})
    time.sleep(2)  # give the device a moment to finalize the saved file

    print("  Fetching latest image info ...")
    info = run_action(cfg, dtype, dnum, "get_last_image", {"is_subframe": False, "is_thumb": False})
    if not info or not info.get("url"):
        print("  [!] No image URL returned -- nothing to download.")
        return

    url = info["url"]
    name = info.get("name") or time.strftime("seestar_%Y%m%d_%H%M%S")
    if not name.lower().endswith((".jpg", ".jpeg", ".png")):
        name += ".jpg"

    print(f"  Downloading image from the Seestar to this laptop ...")
    try:
        os.makedirs(IMAGES_DIR, exist_ok=True)
        local_path = os.path.join(IMAGES_DIR, name)
        with urllib.request.urlopen(url, timeout=30) as resp, open(local_path, "wb") as f:
            f.write(resp.read())
    except Exception as exc:
        print(f"  [FAILED] Could not download image: {describe_error(exc)}")
        print(f"  (Tried: {url})")
        return

    print(f"  [OK] Saved image locally: {local_path}")


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def change_ip(cfg):
    current = cfg["ip"]
    new_ip = input(f"Enter new IP address [{current}]: ").strip()
    if new_ip:
        cfg["ip"] = new_ip
        save_config(cfg)
        print(f"  IP updated to {cfg['ip']}")
    else:
        print("  Unchanged.")


def change_port(cfg):
    current = cfg["port"]
    new_port = input(f"Enter new port [{current}]: ").strip()
    if new_port:
        try:
            cfg["port"] = int(new_port)
            save_config(cfg)
            print(f"  Port updated to {cfg['port']}")
        except ValueError:
            print("  Invalid port, unchanged.")
    else:
        print("  Unchanged.")


# ---------------------------------------------------------------------------
# Menu
# ---------------------------------------------------------------------------

def print_menu(cfg, state):
    print("\n" + "=" * 50)
    print("Seestar S30 Pro - ASCOM Alpaca Control")
    print(f"Bridge: {base_url(cfg)}")
    tele = state.get("telescope")
    print(f"Telescope: {'#' + str(tele[1]) if tele else 'not found'}")
    print("=" * 50)
    print(" 1. Change bridge IP address")
    print(" 2. Change bridge port")
    print(" 3. Test connection / discover devices")
    print(" 4. Connect telescope")
    print(" 5. Open arm (startup sequence)")
    print(" 6. Close arm (park -- currently a driver stub)")
    print(" 7. Go to target")
    print(" 8. Take image (stack + download to this laptop)")
    print(" 9. Exit")


def main():
    cfg = load_config()
    state = {"devices": [], "telescope": None}

    while True:
        print_menu(cfg, state)
        choice = input("Choose an option: ").strip()

        if choice == "1":
            change_ip(cfg)
        elif choice == "2":
            change_port(cfg)
        elif choice == "3":
            test_connection(cfg, state)
        elif choice == "4":
            connect_devices(cfg, state)
        elif choice == "5":
            open_arm(cfg, state)
        elif choice == "6":
            close_arm(cfg, state)
        elif choice == "7":
            goto_target(cfg, state)
        elif choice == "8":
            take_image(cfg, state)
        elif choice == "9":
            print("Goodbye.")
            sys.exit(0)
        else:
            print("Invalid option, try again.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
