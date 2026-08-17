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
Switch / Settings / Diagnostics), a resizable status/log pane at the bottom
(everything the backend would normally print shows up here in near-real-
time, with an Autoscroll toggle so fast output doesn't scroll past you),
a row of colored connection lights per device (green = connected, red =
not connected, amber = unknown/error) that refresh after every action, top
-bar buttons for Discover / Connect All / Disconnect All, and a Browse...
button on the Camera tab to choose where FITS/PNG files get saved. All
Alpaca calls run on a single background worker thread so the window never
freezes and no two requests race each other.

**Configuration is done through the GUI, not by hand-editing JSON:**

```
run_gui.bat
  -> Settings tab
     -> select your ASTAP executable (or click "Detect Automatically")
     -> select your output/image folder
     -> set Slew & Center defaults (tolerance, iterations, exposure,
        minimum altitude, Sun exclusion radius)
     -> Save Settings
```

Every field is validated before it's saved (bad values are rejected with a
clear message, never silently written). `config.json` is created/updated
by this flow and is gitignored, since it ends up holding your local ASTAP
path and save folder. Manually editing `config.json` (start from
`config.example.json`) remains fully supported as an advanced option --
the console app reads the same file.

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

## Slew & Center (plate-solve loop)

Reproduces NINA's Slew -> Expose -> Plate Solve -> Sync -> Reslew workflow
using only native Alpaca (`SlewToCoordinatesAsync`, native Camera exposure,
`SyncToCoordinates`) plus [ASTAP](http://www.hnsky.org/astap.htm) for plate
solving. The backend (`centering.py`) has no Tkinter dependency -- the GUI
(Telescope tab's "Slew & Center" section) and the console (menu `60`) call
the exact same function.

**Requires ASTAP** (a free, separate program -- this project does not
download or bundle it): install it yourself, then either let Settings'
"Detect Automatically" find it (checks PATH and the common Windows install
locations, `C:\Program Files\astap\astap_cli.exe` / `astap.exe`), or Browse
to it directly, then click "Test ASTAP" to confirm it runs and see its
version. Detection never overwrites an already-configured path silently --
you're always asked before it's applied.

The ASTAP CLI flags used here (`-f -r -fov -ra -spd -z -update -wcs`) and
the success/failure signal (the `.ini` sidecar file's `PLTSOLVD=T/F` --
**not** exit code, which is confirmed live to be `0` even when ASTAP finds
zero stars) come from running the actual installed `astap_cli.exe -h` and a
real solve attempt during development, not secondhand documentation --
see `platesolver.py`'s module docstring for exactly what's live-verified
versus grounded-but-not-yet-solved-a-real-star-field.

**Workflow:** slew to the target -> expose -> plate solve -> measure the
true spherical pointing error (not a naive RA/Dec subtraction -- see
`centering.angular_separation_deg`) -> if within tolerance, done; otherwise
`SyncToCoordinates` if the driver advertises `CanSync` (confirmed `True` on
this hardware), or a computed corrected reslew if it doesn't (or if the
Sync call itself fails at runtime) -> repeat, up to a configurable
iteration cap. The first iteration gets one bounded wide/blind-search retry
(larger radius, no unbounded blind search) since a cold-boot first slew can
be significantly off.

**Safety, checked before any slew:** target altitude (rejected below
`minimum_target_altitude_deg`) and Sun separation (rejected within
`sun_exclusion_deg`), computed from the *telescope's own* UTC and site
lat/long (not the laptop's clock) via `astropy.coordinates`. A target that
fails either check is never slewed to.

FITS files written during centering get real metadata where available
(`EXPTIME`, `DATE-OBS`, `RA`/`DEC`/`OBJCTRA`/`OBJCTDEC` from what the
telescope actually reported, `SITELAT`/`SITELONG`, `INSTRUME`,
`TELESCOP`) via `imaging.build_fits_header_items()` -- nothing invented for
fields that couldn't be read.

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

## Daylight Alpaca / NINA Compatibility Test

Menu `80` in the console app, or `python daylight_capability_test.py`
standalone. Answers one question without needing dark skies: **does this
S30 Pro's native Alpaca server expose what a NINA-style Slew -> Expose ->
Plate Solve -> Sync -> Reslew workflow needs?** It does NOT do any plate
solving itself -- it only checks whether the prerequisites exist:
`CanSlewAsync`, `CanSync`/`CanSyncAltAz`, a working native Camera interface,
an accurate/writable `UTCDate`, and a known/writable site location.

Read-only except for three explicitly opt-in prompts (write-back-tests
UTCDate and site lat/long/elevation without changing their real values, and
an optional brief `MoveAxis` pulse to confirm physical response) -- it never
takes an exposure, never slews to a real target, and never touches the arm
unless you type `y`. Saves both a human-readable
`logs/daylight_capability_<timestamp>.log` and a machine-readable
`logs/daylight_capability_<timestamp>.json` (full property dump plus a
`nina_compatibility` verdict) every run.

Confirmed live, 2026-08-17, firmware `1.2.0-3`: `CanSync=True`,
`CanSyncAltAz=False`, `CanSlewAsync=True`, `CanSlewAltAzAsync=False`
(AltAz slews must go through `SlewToCoordinatesAsync` in RA/Dec, there's no
native AltAz slew), `UTCDate` and `SiteLatitude`/`SiteLongitude` are
writable (`SiteElevation` is not implemented at all), both cameras report
full capability data once connected. Classified **likely_viable** for the
NINA-style workflow -- night-time plate-solving validation is the next
step, not yet implemented here.

## Layout

```
main.py               interactive console app / menu
gui.py                 Tkinter GUI (same backend as main.py; Settings tab,
                        Slew & Center section)
daylight_capability_test.py   NINA-compatibility capability test
centering.py             Slew & Center backend (no Tkinter): SlewToCoordi-
                          natesAsync -> expose -> solve -> Sync/corrected
                          reslew loop, angular_separation_deg(),
                          check_target_safety() (altitude + Sun exclusion)
platesolver.py           ASTAP CLI wrapper: find_astap(), test_astap(),
                          solve_fits() -- structured results, GUI/console
                          never parse ASTAP output themselves
timeutil.py               shared Alpaca UTCDate parsing
config.py               config.json load/save + validate_settings()
                         (Tkinter-free, unit tested, shared by gui.py's
                         Save Settings and by tests/)
config.example.json      copy to config.json to customize, or use the
                          GUI's Settings tab instead
diagnostics.py          capability report, verbose telescope init sequence,
                         arm/movement diagnostic suite, report_action()/
                         try_action() OK/FAILED formatters
imaging.py               exposure workflow, FITS writer + build_fits_
                          header_items(), PNG preview/debayer
alpaca/
  device.py              AlpacaDevice base: transactions, error checking,
                          DEBUG logging (ImageArray payloads are summarized,
                          never dumped in full)
  discovery.py            UDP alpacadiscovery1 broadcast
  server.py               AlpacaServer: /management endpoints, device factory
  telescope.py, camera.py, focuser.py, filterwheel.py, switch.py
                           typed wrappers, one per ASCOM Alpaca device interface
tests/                   pytest suite -- config, platesolver, centering;
                          no physical hardware or ASTAP install required
                          (see Testing below)
logs/                    timestamped DEBUG traffic logs, one per run (gitignored)
images/                  default FITS + PNG output folder (gitignored)
legacy_bridge_reference/  old seestar_alp-based script, kept for reference only
```

## Testing

```bash
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest tests/ -v
```

No physical S30 Pro, network access, or ASTAP install required -- Alpaca
devices and ASTAP's subprocess calls are replaced with fakes/mocks. The
ASTAP `.ini`-format and `-h` version-string tests are built from real
output captured by running the actual installed `astap_cli.exe` during
development (see `platesolver.py`'s docstring); the solved-header test
constructs its own FITS header rather than relying on a real star-field
solve, since none was available in daylight.
