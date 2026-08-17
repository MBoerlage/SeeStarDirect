"""
ASTAP plate-solve backend. The GUI and console never parse ASTAP's raw
output themselves -- everything goes through solve_fits()/find_astap()/
test_astap() here.

Grounded in the REAL astap_cli.exe (`-h` output) run live on this project's
own dev machine, and a live test run against a synthetic unsolvable FITS
file, 2026-08-17 -- not guessed from secondhand docs:

    ASTAP astrometric solver version CLI-2026.07.16
    -f  filename
    -r  radius_area_to_search[degrees]
    -fov diameter_field[degrees] {enter zero for auto}
    -ra  right_ascension[hours]
    -spd south_pole_distance[degrees]
    -z  downsample_factor[0,1,2,3,4,..] {0 = auto}
    -update  {Add the solution to the input fits file header}
    -wcs  {Write a .wcs file}
    Solver result will be written to filename.ini and filename.wcs.

Confirmed live: exit code is 0 EVEN ON TOTAL FAILURE (0 stars found), so
exit code alone is NOT a valid success signal. The one reliable, tested
signal is the sidecar <basename>.ini file:

    PLTSOLVD=F
    CMDLINE="..."
    ERROR=Not enough stars.
    WARNING=Warning, remaining image dimensions too low!

On success, PLTSOLVD=T and (per -update, a documented, not-yet-live-tested
behavior) the solved WCS keywords (CRVAL1/CRVAL2 in degrees, CROTA2,
CDELT1/2 or CD matrix) are written into the FITS header itself -- read back
via astropy.io.fits rather than re-parsing ASTAP's text output, since WCS
FITS keywords are a well-defined standard. Treat solve_fits()'s SUCCESS
path as grounded-but-not-yet-live-verified (no real star field image was
available to solve during development) until it's run against a real
S30 Pro exposure -- the FAILURE path above is fully live-verified.
"""

import os
import re
import shutil
import subprocess
import time

try:
    from astropy.io import fits as _fits
except ImportError:
    _fits = None

COMMON_WINDOWS_PATHS = [
    r"C:\Program Files\astap\astap_cli.exe",
    r"C:\Program Files\astap\astap.exe",
    r"C:\Program Files (x86)\astap\astap_cli.exe",
    r"C:\Program Files (x86)\astap\astap.exe",
]


def find_astap():
    """Best-effort auto-detection. Returns a path string, or None. Checks
    PATH first, then common Windows install locations."""
    for name in ("astap_cli", "astap_cli.exe", "astap", "astap.exe"):
        found = shutil.which(name)
        if found:
            return found
    for path in COMMON_WINDOWS_PATHS:
        if os.path.isfile(path):
            return path
    return None


def is_valid_astap_path(path):
    return bool(path) and os.path.isfile(path)


def test_astap(path, timeout=8):
    """Runs `<path> -h` (harmless, no image processing) to confirm the
    executable launches and to read its version string. Returns
    {"ok": bool, "message": str, "version": str or None}."""
    if not is_valid_astap_path(path):
        return {"ok": False, "message": f"Not a file: {path}", "version": None}
    try:
        proc = subprocess.run([path, "-h"], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": f"Timed out after {timeout}s (no response to -h)",
                "version": None}
    except OSError as e:
        return {"ok": False, "message": f"Could not launch: {e}", "version": None}

    output = (proc.stdout or "") + (proc.stderr or "")
    m = re.search(r"version\s+(\S+)", output, re.IGNORECASE)
    version = m.group(1) if m else None
    return {
        "ok": True,
        "message": f"Executable responded (exit code {proc.returncode}).",
        "version": version,
    }


def _failure(message, solve_time=None):
    return {
        "success": False, "message": message,
        "ra_hours": None, "dec_deg": None, "rotation_deg": None,
        "pixel_scale_arcsec": None, "solve_time_seconds": solve_time,
    }


def _parse_ini(path):
    result = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if "=" in line:
                    k, v = line.split("=", 1)
                    result[k.strip().upper()] = v.strip()
    except OSError:
        pass
    return result


def solve_fits(fits_path, astap_path, timeout=60, radius_deg=30, fov_deg=0,
               downsample=2, ra_hint_hours=None, dec_hint_deg=None):
    """Runs ASTAP against fits_path. Returns:
        {success, ra_hours, dec_deg, rotation_deg, pixel_scale_arcsec,
         solve_time_seconds, message}
    Never raises for ordinary solve failures (missing stars, timeout, bad
    path) -- only if astropy itself is missing, which is a setup problem,
    not a solve failure, and is still reported via the same dict shape."""
    if _fits is None:
        return _failure("astropy is not installed -- cannot read the solved FITS header "
                         "(pip install -r requirements.txt)")
    if not is_valid_astap_path(astap_path):
        return _failure(f"ASTAP executable not found: {astap_path!r} -- set it in Settings.")
    if not os.path.isfile(fits_path):
        return _failure(f"Input FITS not found: {fits_path}")

    args = [astap_path, "-f", fits_path, "-r", str(radius_deg), "-fov", str(fov_deg),
            "-z", str(downsample), "-update", "-wcs"]
    if ra_hint_hours is not None and dec_hint_deg is not None:
        spd = 90.0 - float(dec_hint_deg)  # ASTAP wants south-polar-distance, not Dec
        args += ["-ra", str(ra_hint_hours), "-spd", str(spd)]

    t0 = time.time()
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return _failure(f"ASTAP timed out after {timeout}s", time.time() - t0)
    except OSError as e:
        return _failure(f"Could not launch ASTAP: {e}", time.time() - t0)
    solve_time = time.time() - t0

    ini_path = os.path.splitext(fits_path)[0] + ".ini"
    ini_info = _parse_ini(ini_path) if os.path.exists(ini_path) else {}

    # The .ini PLTSOLVD flag is the one signal confirmed live against the
    # real binary -- exit code 0 does NOT mean success (confirmed: ASTAP
    # exits 0 even when it finds zero stars).
    solved_flag = ini_info.get("PLTSOLVD", "").strip().upper()
    if solved_flag == "F":
        err = ini_info.get("ERROR", "solve failed (see .ini)")
        warn = ini_info.get("WARNING")
        msg = f"ASTAP: {err}" + (f" ({warn})" if warn else "")
        return _failure(msg, solve_time)

    if not ini_info and proc.returncode != 0:
        combined = ((proc.stdout or "") + (proc.stderr or "")).strip()
        return _failure(f"ASTAP exited with code {proc.returncode}: {combined[-400:]}", solve_time)

    if solved_flag != "T":
        # No .ini at all, or PLTSOLVD missing/ambiguous -- fall back to
        # checking the FITS header directly before giving up.
        pass

    try:
        header = _fits.getheader(fits_path)
    except Exception as e:
        return _failure(f"Could not read FITS header after solve attempt: {e}", solve_time)

    header_solved = str(header.get("PLTSOLVD", "")).strip().upper() in ("T", "TRUE", "1")
    if solved_flag != "T" and not header_solved:
        tail = ((proc.stdout or "") + (proc.stderr or "")).strip()
        return _failure("ASTAP did not report success (no PLTSOLVD=T in .ini or FITS header). "
                         f"Last output: {tail[-300:]}", solve_time)

    try:
        ra_deg = float(header["CRVAL1"])
        dec_deg = float(header["CRVAL2"])
    except (KeyError, ValueError, TypeError):
        return _failure("Solved but CRVAL1/CRVAL2 missing from FITS header", solve_time)

    rotation_deg = None
    for key in ("CROTA2", "CROTA1"):
        if key in header:
            try:
                rotation_deg = float(header[key])
                break
            except (ValueError, TypeError):
                pass

    pixel_scale_arcsec = None
    for key in ("CDELT2", "CDELT1"):
        if key in header:
            try:
                pixel_scale_arcsec = abs(float(header[key])) * 3600.0
                break
            except (ValueError, TypeError):
                pass
    if pixel_scale_arcsec is None and "CD1_1" in header and "CD2_1" in header:
        try:
            cd11, cd21 = float(header["CD1_1"]), float(header["CD2_1"])
            pixel_scale_arcsec = ((cd11 ** 2 + cd21 ** 2) ** 0.5) * 3600.0
        except (ValueError, TypeError):
            pass

    return {
        "success": True, "message": "solved",
        "ra_hours": ra_deg / 15.0, "dec_deg": dec_deg,
        "rotation_deg": rotation_deg, "pixel_scale_arcsec": pixel_scale_arcsec,
        "solve_time_seconds": solve_time,
    }
