# SeeStarDirect

Direct Python control of ZWO Seestar telescopes using their native ASCOM
Alpaca interface.

SeeStarDirect talks directly to the Alpaca server running inside the
telescope -- no NINA, ASCOM COM driver, `seestar_alp` bridge, or
reverse-engineered Seestar protocol required.

```
SeeStarDirect GUI
       |
       | ASCOM Alpaca HTTP
       v
Seestar native Alpaca server
       |
       +-- Telescope
       +-- Camera(s)
       +-- Focuser(s)
       +-- Filter Wheel
       +-- Switch
```

A Tkinter **GUI is the primary way to use this project** (`gui.py` /
`run_gui.bat`). An **advanced/diagnostic console** (`main.py` / `run.bat`)
remains available for scripting and low-level debugging -- both are thin
front-ends over one shared backend (`alpaca/`, `centering.py`,
`platesolver.py`, `diagnostics.py`, `imaging.py`, `config.py`), so nothing
is duplicated between them and anything the console can do, the GUI can
too.

Device numbers/names/exposure limits shown anywhere in this README are
what the S30 Pro used during development reported -- SeeStarDirect
discovers everything dynamically, so don't assume your unit matches
exactly.

## Quick start

```
1. Install:      python -m venv .venv
                  .venv\Scripts\pip install -r requirements.txt
2. Put the Seestar in Station Mode, on the same LAN as this computer.
3. Run:           run_gui.bat
4. Click Discover.
5. Settings tab -> configure ASTAP path / output folder / Slew & Center
   defaults -> Save Settings (optional for basic control; required for
   Slew & Center).
6. Telescope / Camera / Focuser / Filter Wheel / Switch tabs -> control
   the telescope.
```

No IP address to type in by hand -- Discover uses standard Alpaca UDP
discovery (`alpacadiscovery1` broadcast on port 32227).

## GUI overview

| Tab | What it does |
|---|---|
| **Telescope** | Connect, state readout, Initialize/Park, tracking, a simple RA/Dec slew, manual axis movement test, and the **Slew & Center** plate-solve loop with a live progress panel. |
| **Camera** | Connect either camera, take a test exposure, save FITS + PNG preview, choose the output folder. |
| **Focuser** | Connect, show position/limits/temperature, move to an absolute position, halt. |
| **Filter Wheel** | Connect, list filters by their real names, select one. |
| **Switch** | Connect, list switches (e.g. the dew heater) by real name, toggle writable ones. |
| **Settings** | All persistent configuration -- see below. The central place to configure the app; you shouldn't need to hand-edit `config.json` for normal use. |
| **Diagnostics / Raw** | Full capability report, `SupportedActions` dump, device info dump, and raw Alpaca GET/PUT for poking any endpoint by hand. |

A row of colored connection lights (green = connected, red = not
connected, amber = unknown/error) sits above the tabs and refreshes after
every action. A resizable, scrollable status/log pane at the bottom mirrors
everything the backend prints, with an Autoscroll toggle so fast output
doesn't scroll past you before you can read it. Every tab's content
scrolls (mouse wheel or the scrollbar) so nothing gets clipped at small
window sizes. All Alpaca calls run on a single background worker thread --
the window never freezes, and no two requests race each other.

*(Screenshots would go here once some exist in the repo -- none are
included yet, and none are fabricated.)*

## Settings tab

| Section | Fields |
|---|---|
| **Network** | Client ID, discovery timeout, preferred-server IP/port fallback (used **only** if UDP discovery finds nothing -- the Alpaca port itself always comes from discovery, never from this setting). |
| **Files** | Output/image folder (shared with the Camera tab -- one setting, not two). |
| **Plate Solving** | ASTAP executable path (Browse / Detect Automatically / Test ASTAP), plate-solve timeout. |
| **Slew & Center** | Centering camera (picked from real discovered camera names), exposure, tolerance, maximum iterations, minimum altitude, Sun exclusion radius. |
| **Observing Site** | Optional fallback latitude/longitude, used only if the telescope's own `SiteLatitude`/`SiteLongitude` can't be read. The telescope's live values are always preferred. |

Every field is validated before it's saved -- a bad value (non-numeric,
out of range, etc.) is rejected with a clear message and **nothing is
written**; SeeStarDirect never partially applies a bad batch of settings.
Most changes take effect immediately, no restart needed (client ID and
discovery timeout are the exception -- they apply starting with the next
Discover, since already-connected devices keep the client ID they were
created with).

## Configuration architecture

`config.example.json` is the tracked reference/defaults file -- copy it to
`config.json` if you want to hand-edit configuration instead of using
Settings. `config.json` itself is **gitignored** and never committed,
since it ends up holding your local ASTAP path and save folder. Settings
you choose in the GUI are written there automatically. No machine-specific
path, IP, or coordinate is committed to the repository.

## ASTAP setup

ASTAP is only required for **Slew & Center**, the daylight capability
test's optional deeper checks, and any other plate-solving feature.
**Basic Telescope / Camera / Focuser / Filter Wheel / Switch control works
without it.**

```
Settings
  -> Plate Solving
     -> Browse...  (select astap.exe / astap_cli.exe)
        or Detect Automatically (checks PATH and the common Windows
        install locations -- never overwrites a saved path without
        asking first)
     -> Test ASTAP  (confirms it launches and shows its version)
     -> Save Settings
```

[ASTAP](http://www.hnsky.org/astap.htm) is a free, separate program --
SeeStarDirect does not download, bundle, or auto-install it.

## Telescope workflow

**Simple Slew** -- the Telescope tab's basic RA/Dec section: pick a
catalog target or type RA/Dec, slew, watch state update. No camera or
ASTAP needed.

**Slew & Center** -- reproduces NINA's Slew -> Expose -> Plate Solve ->
Sync -> Reslew workflow using only native Alpaca (`SlewToCoordinatesAsync`,
native Camera exposure, `SyncToCoordinates`) plus ASTAP:

```
RA/Dec GoTo (SlewToCoordinatesAsync)
      |
      v
native Camera exposure -> FITS
      |
      v
ASTAP plate solve
      |
      v
measure true spherical pointing error
      |
      v
within tolerance? --------- yes ---> done
      |
      no
      v
SyncToCoordinates (if CanSync; a computed
corrected reslew otherwise, or if Sync fails)
      |
      v
reslew -> repeat (bounded iteration count)
```

Backend: `centering.py` (no Tkinter dependency -- the GUI's Telescope tab
and the console's menu `60` call the exact same function). Pointing error
is measured as a true spherical separation (`centering.angular_separation_deg`,
via `astropy.coordinates.SkyCoord`), never a naive RA/Dec subtraction --
that would be badly wrong near the poles, where an hour of RA covers a tiny
true angle. The first iteration gets one bounded wide-search retry (larger
radius) since a cold-boot first slew can be significantly off; there is no
unbounded blind search.

**Safety, checked before any slew:** target altitude (rejected below
`minimum_target_altitude_deg`) and Sun separation (rejected within
`sun_exclusion_deg`), computed from the *telescope's own* live UTC and site
lat/long (not the laptop's clock), via `astropy.coordinates`. A target that
fails either check is never slewed to.

FITS files get real metadata where available (`EXPTIME`, `DATE-OBS`,
`RA`/`DEC`/`OBJCTRA`/`OBJCTDEC` from what the telescope actually reported,
`SITELAT`/`SITELONG`, `INSTRUME`, `TELESCOP`) via
`imaging.build_fits_header_items()` -- nothing invented for a field that
couldn't be read.

## Alt-Az: not a limitation

On the tested S30 Pro:

| Capability | Value |
|---|---|
| `CanSlewAsync` | `True` |
| `CanSync` | `True` |
| `CanSlewAltAzAsync` | `False` |
| `CanSyncAltAz` | `False` |

The physical mount is Alt-Az, but ZWO's native Alpaca interface accepts
standard **equatorial** (RA/Dec) slew and Sync operations -- there's no
native Alpaca-level Alt-Az slew/sync on this firmware. SeeStarDirect
therefore operates the mount entirely through standard RA/Dec sky
coordinates and lets the S30 Pro's own firmware handle the physical
Alt-Az mechanics underneath. This matches how NINA and other standard
Alpaca clients would drive it too -- it's not a workaround.

## Alignment: what's confirmed

**Confirmed by direct physical testing:** on a genuine cold boot (power
on, official Seestar app never opened), the S30 Pro's arm/motors respond
to native Alpaca commands and physically slew when given a real
`SlewToCoordinatesAsync`. SeeStarDirect does not require opening the phone
app, a separate two-star alignment step, North calibration, or level
calibration to get the mount responsive and moving via standard Alpaca.

**Not yet established by physical testing:** whether the S30 Pro's
cold-boot pointing accuracy is good enough for ASTAP to solve on the first
try, and whether the `SyncToCoordinates` + reslew loop actually converges
pointing error to something useful. `SyncToCoordinates` itself has been
exercised only in mocked unit tests so far, not against real hardware --
`CanSync=True` (the capability flag) is confirmed live, but the command's
real-world effect on the mount's pointing model is not. This is exactly
what the Slew & Center feature exists to determine on the sky, and is
tracked as experimental until it has been.

## Firmware

| Version | Status |
|---|---|
| `1.1.1-1` | Old, problematic. ZWO-acknowledged bug: arm/motors did not respond to native Alpaca commands after a power cycle until the official Seestar app had connected once. Confirmed live by direct visual observation on this unit. |
| `1.2.0-3` | Current tested version. Auto-updated from `1.1.1-1` (not done from this tool). **Confirmed via a genuine cold-boot test (app never opened) that the arm/motors respond to native Alpaca alone** -- the app-wake workaround is no longer needed on this version. |

If you hit an unresponsive arm on your own unit, update the Seestar app
and telescope firmware first, before debugging external Alpaca control --
`print_software_info()` (run automatically on every Discover) checks the
live driver version and prints a warning only if it's older than
`1.1.3-1`, ZWO's stated fix version; on current firmware nothing prints,
since there is nothing to warn about.

## Validated capabilities

Only claims backed by an actual test against the real S30 Pro (or, where
noted, real ASTAP CLI behavior) are marked Validated. Code existing is not
the same as a capability being validated.

| Capability | S30 Pro `1.2.0-3` |
|---|---|
| UDP discovery | Validated |
| Native Telescope (identity, state, capability flags) | Validated |
| Native Camera 0 / 1 (identity, capabilities, exposure, ImageArray) | Validated |
| Focuser (identity, capabilities) | Validated read-only; `Move` not yet exercised on real hardware |
| Filter Wheel (identity, capabilities) | Validated read-only; position change not yet exercised |
| Switch / dew heater (identity, capabilities) | Validated read-only; toggling not yet exercised |
| RA/Dec slew -- real physical motion | Validated (cold boot, app never opened, native Alpaca alone) |
| `CanSync` capability flag | Validated `True` |
| `SyncToCoordinates` command | Not yet exercised against real hardware (unit-tested with mocks only) |
| Native `ImageArray` / FITS save | Validated |
| ASTAP plate solve | Experimental -- CLI integration grounded in real `astap_cli.exe -h` output and a real (unsolvable, by design) solve attempt; not yet solved a real star field |
| Slew & Center end-to-end loop | Experimental -- unit-tested with mocks/fakes; not yet run against real sky |
| Cold-boot headless arm response (app never opened) | Validated |

## Diagnostics

**Daylight Alpaca / NINA Compatibility Test** -- menu `80` in the console,
or `python daylight_capability_test.py` standalone. Safe to run in
daylight: read-only except three explicitly opt-in prompts (non-destructive
write-back tests for `UTCDate` and site lat/long, and an optional brief
`MoveAxis` pulse). Answers whether the prerequisites for a NINA-style
workflow exist, without doing any plate solving itself. Confirmed live,
firmware `1.2.0-3`:

```
CanSlewAsync = YES         CanSync = YES
CanSyncAltAz = NO          CanSlewAltAzAsync = NO
UTCDate: read/write both work
SiteLatitude/SiteLongitude: read/write both work
SiteElevation: NotImplemented
Two Cameras available, full capability data once connected
Telescope SupportedActions = []  (no hidden ZWO alignment action)
```

Saves both a human-readable `logs/daylight_capability_<timestamp>.log` and
a machine-readable `.json` (full property dump plus a `nina_compatibility`
verdict) every run.

**Cold-boot headless validation** -- not a separate script (there's
nothing to automate about power-cycling hardware), but the procedure that
established the Alignment/Firmware findings above:

```
1. Power the S30 Pro off completely.
2. Do NOT open the Seestar mobile app.
3. Power on, wait for Station Mode.
4. run_gui.bat -> Discover -> Connect telescope.
5. Telescope tab -> Simple Slew (or Slew & Center) -> watch the physical
   unit.
```

If you repeat this on a different unit or firmware version and get a
different result, that's worth filing as an issue -- the finding above is
specific to the unit and firmware version tested, not assumed to hold
universally.

## Advanced / diagnostic console

`main.py` / `run.bat` -- a full-featured text menu covering every GUI
capability plus lower-level tools the GUI doesn't expose a dedicated
button for (e.g. `92`/`93` raw Alpaca GET/PUT against any endpoint by
hand). Useful for scripting, headless boxes, or comparing exactly what
was sent/received when something doesn't behave as expected -- every
request/response is in `logs/seestar_alpaca_<timestamp>.log` at DEBUG
level.

## ZWO-specific quirks (not part of the ASCOM spec)

- `SupportedActions` is `[]` (empty) on every device except Switch, which
  reports `['OCHTestPowerReport']` -- a ZWO-proprietary action, not wrapped
  by this code (use the raw PUT diagnostic if you need it).
- Switch 0's `Name`/`Description`/`DriverInfo` report `"Alpaca Switch
  Simulator"` / `"ASCOM SwitchV2 Simulator Driver."` -- ZWO appears to have
  built this device directly on ASCOM's reference Switch sample driver and
  didn't rebrand those particular strings (Telescope/Camera/Focuser/
  Filter Wheel all correctly report "Seestar S30 Pro...").
- `SideOfPier` (Telescope), `Gains`/`Offset` (Camera), `StepSize`
  (Focuser), `SiteElevation` (Telescope) all return ErrorNumber 1024
  (NotImplemented) -- expected for an alt-az mount / this sensor, not
  bugs.
- Both cameras report `SensorType=2` (RGGB Bayer) -- `ImageArray` returns
  the raw, undebayered mosaic. `imaging.py` preserves that untouched in
  the FITS output and does a quick, low-quality 2x2 debayer only for the
  PNG preview.

## Repository layout

```
gui.py                    Tkinter GUI -- primary interface
main.py                   advanced/diagnostic console
config.py                 config.json load/save + validate_settings()
                           (Tkinter-free, unit tested, shared by the GUI's
                           Save Settings and by tests/)
config.example.json       tracked defaults/reference -- copy to config.json,
                           or use the GUI's Settings tab instead
diagnostics.py             capability reports, verbose telescope init
                            sequence, arm/movement diagnostic suite
imaging.py                  exposure workflow, FITS writer +
                             build_fits_header_items(), PNG preview/debayer
platesolver.py                ASTAP CLI wrapper: find_astap(), test_astap(),
                               solve_fits() -- structured results, GUI/
                               console never parse ASTAP output themselves
centering.py                    Slew & Center backend (no Tkinter):
                                 SlewToCoordinatesAsync -> expose -> solve
                                 -> Sync/corrected reslew loop,
                                 angular_separation_deg(),
                                 check_target_safety()
daylight_capability_test.py   NINA-compatibility capability test
timeutil.py                     shared Alpaca UTCDate parsing
alpaca/
  device.py                     AlpacaDevice base: transactions, error
                                 checking, DEBUG logging (ImageArray
                                 payloads summarized, never dumped in full)
  discovery.py                   UDP alpacadiscovery1 broadcast
  server.py                      AlpacaServer: /management endpoints,
                                  device factory
  telescope.py, camera.py, focuser.py, filterwheel.py, switch.py
                                  typed wrappers, one per ASCOM Alpaca
                                  device interface
tests/                    pytest suite -- config, platesolver, centering;
                           no physical hardware or ASTAP install required
logs/                      timestamped DEBUG traffic logs (gitignored)
images/                    default FITS + PNG output folder (gitignored)
legacy_bridge_reference/  old seestar_alp-based script, kept for reference
                          only -- not part of this architecture
```

## Setup

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

`run_gui.bat` (primary) or `run.bat` (console). Both check the venv
exists and (GUI) that dependencies actually import before launching, with
a clear message and a `pause` rather than a silent failure if something's
missing.

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

## License

No LICENSE file yet. MIT is a reasonable default for a project like this
(permissive, minimal friction for other Seestar owners to use/fork it),
but that's a recommendation, not a decision made on the maintainer's
behalf -- add a `LICENSE` file explicitly when you're ready to choose one.

## Project status

Reliable today: GUI startup, discovery, device control (Telescope/Camera
confirmed on real hardware; Focuser/Filter Wheel/Switch confirmed
read-only), Settings persistence and validation, FITS capture. Experimental
and not yet validated end-to-end on real sky: ASTAP solving and the full
Slew & Center loop. See Validated capabilities above for the precise
breakdown before relying on anything for real observing.
