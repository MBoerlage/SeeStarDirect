# Seestar S30 Pro -- Native ZWO Alpaca Controller

Talks directly to the S30 Pro's own native ASCOM Alpaca server over HTTP.
No bridge, no localhost proxy, no port-4700 proprietary protocol, no ASCOM
COM driver, no NINA. NINA (or this program) are just two Alpaca clients
talking to the same server -- this program does the same discovery and
device control NINA does. Console app (`main.py`) and GUI (`gui.py`) share
one backend (`alpaca/`, `diagnostics.py`, `imaging.py`) with no duplicated
logic between them.

## ⚠️ Firmware requirement

**Your Seestar needs Alpaca/driver firmware `1.1.3-1` or newer.**

Older firmware (confirmed on `1.1.1-1`) has a ZWO-acknowledged bug: after
a power cycle, the arm/motor subsystem does not respond to *any* native
Alpaca client -- this tool, NINA, whatever -- until the official Seestar
mobile app has connected once. Commands like `Unpark`, `FindHome`,
`MoveAxis`, and `SlewToCoordinatesAsync` all report success
(`ErrorNumber=0`) and even update the driver's self-reported RA/Dec/Alt/Az
coordinates, but produce **zero physical motion**. This was confirmed live
by direct visual observation, not assumed.

**How to check your version:** run this tool and hit Discover (menu `1` in
the console app, or the Discover button in the GUI) -- it prints the
server and Telescope driver version automatically and prints a loud
warning if it's older than `1.1.3-1`.

**If you're stuck on older firmware:** open the official Seestar app once
after powering the unit on, let it connect (the arm should rise), then
close the app. Native Alpaca control -- this tool or NINA -- then works
normally for the rest of that power cycle. This is a one-time-per-power-
cycle workaround, not an ongoing dependency on the app.

**Status on the unit this was developed against:** started on `1.1.1-1`
(bug present, workaround required, confirmed above). It has since
auto-updated to `1.2.0-3` (past the fix version) -- not yet re-verified
whether the workaround is still needed on that version; test with a fresh
power-cycle and no app connection before assuming either way.

```
Python application (this repo)
      |
      | HTTP / REST -- ASCOM Alpaca
      v
Seestar S30 Pro native Alpaca server (port 32323 on the units tested)
      |
      +-- Telescope 0
      +-- Camera 0    (Telephoto, 2160x3840, 2.9um px, ExposureMax=2000s)
      +-- Camera 1    (Wide Angle, 2160x3840, 1.6um px, ExposureMax=60s)
      +-- Focuser 0   (Telephoto,  MaxStep=2600)
      +-- Focuser 1   (Wide Angle, MaxStep=1023)
      +-- FilterWheel 0  (Dark / IR / LP)
      +-- Switch 0    (1 switch: "Dew heater")
```

Device numbers/names/exposure limits above are what THIS unit reported --
discover dynamically rather than assuming these match yours.

The legacy `seestar_alp`-bridge-based script from an earlier iteration of
this project lives in `legacy_bridge_reference/` for reference only. It is
not part of this architecture and is not used by anything here.

## Setup

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

Then run either the console app (`run.bat` / `.venv\Scripts\python main.py`)
or the GUI (`run_gui.bat` / `.venv\Scripts\python gui.py`).

The GUI has a tabbed layout (Telescope / Camera / Focuser / Filter Wheel /
Switch / Diagnostics), a resizable status/log pane at the bottom
(everything the backend would normally print shows up here in near-real-
time, with an Autoscroll toggle so fast output doesn't scroll past you),
a row of colored connection lights per device (green = connected, red =
not connected, amber = unknown/error) that refresh after every action, top
-bar buttons for Discover / Connect All / Disconnect All, and a Browse...
button on the Camera tab to choose where FITS/PNG files get saved. All
Alpaca calls run on a single background worker thread so the window never
freezes and no two requests race each other.

Copy `config.example.json` to `config.json` to customize `client_id`,
`discovery_timeout`, a manual `preferred_server_ip`/`port` fallback (used
only if UDP discovery finds nothing), or `output_directory`. `config.json`
is gitignored since the GUI writes your chosen save folder into it.

## Acceptance test

1. Turn on the S30 Pro, put it in Station Mode, confirm it's on the same
   LAN as this computer.
2. If your firmware is older than `1.1.3-1` (see above), open the Seestar
   app once, let it connect and raise the arm, then close it. Do not start
   NINA or run any bridge.
3. `run.bat` (or `run_gui.bat`)
4. Discover. Should print the IP/port it found, the full device list, and
   the software/version info -- without you typing an IP.
5. Show all capabilities. Confirms every device answers and shows exactly
   what it supports.
6. Connect telescope.
7. Auto Start / First Slew Test (console menu `18`, or the Telescope tab's
   Slew section in the GUI). Watch the physical unit -- this is the one
   confirmed to produce real motion. `Initialize/Unpark+FindHome` and the
   MoveAxis test are also available but do NOT by themselves wake the arm
   from a fresh power-cycle on buggy firmware.
8. Connect a camera, take a test exposure, save it as FITS + PNG.
9. Park, if you want to stow it again.

If any step doesn't do what you expect, the raw GET/PUT diagnostic (menu
`92`/`93`, or the Diagnostics tab in the GUI) lets you poke the exact same
endpoint by hand, and every request/response is in
`logs/seestar_alpaca_<timestamp>.log` at DEBUG level for comparison
against what NINA sends.

## What's confirmed vs. what's still open

**Confirmed working, standard ASCOM Alpaca behavior:**
- UDP discovery (`alpacadiscovery1` broadcast on port 32227).
- `/management/apiversions`, `/management/v1/description`,
  `/management/v1/configureddevices` -- all standard, all working.
- Full property/capability read-out on every device type (see
  `diagnostics.py` / `alpaca/*.py` docstrings for the exact confirmed
  property list per device).
- `Connected` get/set on every device.
- Once the arm has been woken (see Firmware requirement above),
  `SlewToCoordinatesAsync` and `MoveAxis` produce real, visually-confirmed
  physical motion via native Alpaca alone.

**ZWO-specific quirks observed (not part of the ASCOM spec, just what this
particular firmware does):**
- `SupportedActions` is `[]` (empty) on every device except Switch, which
  reports `['OCHTestPowerReport']` -- a ZWO-proprietary action, not wrapped
  by this code (use the raw PUT diagnostic if you need it).
- Switch 0's `Name`/`Description`/`DriverInfo` report `"Alpaca Switch
  Simulator"` / `"ASCOM SwitchV2 Simulator Driver."` -- ZWO appears to have
  built this device directly on ASCOM's reference Switch sample driver and
  didn't rebrand those particular strings (Telescope/Camera/Focuser/
  FilterWheel all correctly report "Seestar S30 Pro...").
- `SideOfPier` (Telescope), `Gains`/`Offset` (Camera), `StepSize`
  (Focuser) all return ErrorNumber 1024 (NotImplemented) -- expected for an
  alt-az mount / this sensor, not bugs.
- Both cameras report `SensorType=2` (RGGB Bayer) -- `ImageArray` returns
  the raw, undebayered mosaic. `imaging.py` preserves that untouched in the
  FITS output and does a quick, low-quality 2x2 debayer only for the PNG
  preview.

## Layout

```
main.py               interactive console app / menu
gui.py                 Tkinter GUI (same backend as main.py)
config.py               config.json load/save
config.example.json      copy to config.json to customize
diagnostics.py          capability report, verbose telescope init sequence,
                         arm/movement diagnostic suite, report_action()/
                         try_action() OK/FAILED formatters
imaging.py               exposure workflow, FITS writer, PNG preview/debayer
alpaca/
  device.py              AlpacaDevice base: transactions, error checking,
                          DEBUG logging (ImageArray payloads are summarized,
                          never dumped in full)
  discovery.py            UDP alpacadiscovery1 broadcast
  server.py               AlpacaServer: /management endpoints, device factory
  telescope.py, camera.py, focuser.py, filterwheel.py, switch.py
                           typed wrappers, one per ASCOM Alpaca device interface
logs/                    timestamped DEBUG traffic logs, one per run (gitignored)
images/                  default FITS + PNG output folder (gitignored)
legacy_bridge_reference/  old seestar_alp-based script, kept for reference only
```
