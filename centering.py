"""
UI-independent Slew & Center backend: SlewToCoordinatesAsync -> expose ->
plate solve -> measure true spherical pointing error -> if outside
tolerance, SyncToCoordinates (or, if CanSync is False, an iterative
pointing-correction offset) -> reslew -> repeat.

No Tkinter here -- gui.py and main.py both call slew_and_center()
identically. gui.py marshals progress_cb callbacks onto its Tk thread
itself; this module knows nothing about that.

Uses only standard native Alpaca (Telescope + Camera) plus platesolver.py
(ASTAP). No seestar_alp, no port 4700, no proprietary protocol.
"""

import os
import threading
import time
from datetime import datetime

from alpaca.device import AlpacaError
import imaging
import platesolver
from timeutil import parse_alpaca_datetime

try:
    import astropy.units as u
    from astropy.coordinates import AltAz, EarthLocation, SkyCoord, get_sun
    from astropy.time import Time
    _HAVE_ASTROPY = True
except ImportError:
    _HAVE_ASTROPY = False


def angular_separation_deg(ra1_hours, dec1_deg, ra2_hours, dec2_deg):
    """Correct spherical-coordinate separation -- NOT a naive RA/Dec
    subtraction, which is wrong near the poles and ignores that an hour of
    RA is a different angular distance at every declination."""
    if _HAVE_ASTROPY:
        c1 = SkyCoord(ra=ra1_hours * u.hourangle, dec=dec1_deg * u.deg)
        c2 = SkyCoord(ra=ra2_hours * u.hourangle, dec=dec2_deg * u.deg)
        return float(c1.separation(c2).deg)

    # Fallback great-circle (haversine/law-of-cosines) distance, used only
    # if astropy is somehow missing at runtime despite being a declared
    # dependency -- kept correct-by-construction, not a placeholder.
    import math
    ra1, dec1 = math.radians(ra1_hours * 15.0), math.radians(dec1_deg)
    ra2, dec2 = math.radians(ra2_hours * 15.0), math.radians(dec2_deg)
    cos_d = (math.sin(dec1) * math.sin(dec2) +
             math.cos(dec1) * math.cos(dec2) * math.cos(ra1 - ra2))
    cos_d = min(1.0, max(-1.0, cos_d))
    return math.degrees(math.acos(cos_d))


def check_target_safety(telescope, ra_hours, dec_deg, min_altitude_deg, sun_exclusion_deg,
                         fallback_lat=None, fallback_lon=None):
    """Returns (ok: bool, reason: str). Prefers the TELESCOPE's own live
    UTC/site (not the laptop's) so the check matches what the mount will
    actually see when it slews there. fallback_lat/fallback_lon (from
    Settings' Observing Site section) are used ONLY if the telescope's own
    SiteLatitude/SiteLongitude can't be read -- they never override a
    working live reading."""
    if not _HAVE_ASTROPY:
        return False, "astropy is not installed -- cannot verify altitude/Sun safety"

    try:
        utc_raw = telescope.get("utcdate")
    except AlpacaError as e:
        return False, f"Could not read UTC from telescope: {e}"

    try:
        lat = telescope.get("sitelatitude")
        lon = telescope.get("sitelongitude")
    except AlpacaError as e:
        if fallback_lat is not None and fallback_lon is not None:
            lat, lon = fallback_lat, fallback_lon
        else:
            return False, (f"Could not read site location from telescope ({e}), and no "
                            f"fallback latitude/longitude is configured in Settings.")

    obs_time = parse_alpaca_datetime(utc_raw)
    if obs_time is None:
        return False, f"Could not parse telescope UTCDate: {utc_raw!r}"

    location = EarthLocation(lat=lat * u.deg, lon=lon * u.deg, height=0 * u.m)
    frame = AltAz(obstime=Time(obs_time), location=location)
    target_altaz = SkyCoord(ra=ra_hours * u.hourangle, dec=dec_deg * u.deg).transform_to(frame)
    altitude = float(target_altaz.alt.deg)

    if altitude < min_altitude_deg:
        return False, f"Target altitude {altitude:.1f} deg is below minimum {min_altitude_deg} deg"

    sun_altaz = get_sun(Time(obs_time)).transform_to(frame)
    separation = float(target_altaz.separation(sun_altaz).deg)
    if separation < sun_exclusion_deg:
        return False, (f"Target is only {separation:.1f} deg from the Sun "
                        f"(minimum {sun_exclusion_deg} deg)")

    return True, f"altitude={altitude:.1f} deg, sun_separation={separation:.1f} deg"


def _slew_and_wait(telescope, ra_hours, dec_deg, log, timeout=120, abort_event=None):
    try:
        telescope.slew_to_coordinates_async(ra_hours, dec_deg)
    except AlpacaError as e:
        log(f"SlewToCoordinatesAsync FAILED: ({e.error_number}) {e.error_message}")
        return False
    t0 = time.time()
    while time.time() - t0 < timeout:
        if abort_event is not None and abort_event.is_set():
            log("Aborted while waiting for slew to complete.")
            return False
        try:
            if not telescope.get("slewing"):
                return True
        except AlpacaError:
            return True  # can't confirm Slewing -- don't hang forever on it
        time.sleep(1)
    log(f"Slew did not report complete within {timeout}s.")
    return False


def _resolve_telescope_and_camera(state, camera_number):
    telescope = state.devices.get(("telescope", 0)) or next(
        (d for (t, _), d in state.devices.items() if t == "telescope"), None)
    camera = state.devices.get(("camera", camera_number))
    return telescope, camera


def slew_and_center(state, target_ra_hours, target_dec_deg, camera_number,
                     exposure_seconds, tolerance_arcmin, max_iterations,
                     astap_path, plate_solve_timeout=60,
                     min_altitude_deg=20.0, sun_exclusion_deg=30.0,
                     fallback_lat=None, fallback_lon=None,
                     output_dir=None, progress_cb=None, log=print,
                     abort_event=None):
    """UI-independent. `progress_cb(dict)`, if given, is called after each
    meaningful step with a snapshot of progress -- gui.py marshals this onto
    its Tk thread itself via self.ui(); this function knows nothing about
    Tkinter. `log(str)` receives human-readable narration.
    `abort_event` (threading.Event) is checked between steps so a caller
    (the GUI's Abort button) can stop a run in progress.

    Returns {success, iterations: [...], final_error_arcmin, message}.
    """
    def emit(status, **kw):
        if progress_cb:
            try:
                progress_cb({"status": status, **kw})
            except Exception:
                pass

    def aborted():
        return abort_event is not None and abort_event.is_set()

    telescope, camera = _resolve_telescope_and_camera(state, camera_number)

    if not telescope:
        msg = "No telescope device known -- run Discover first."
        log(msg)
        emit("error", message=msg)
        return {"success": False, "iterations": [], "final_error_arcmin": None, "message": msg}
    if not camera:
        msg = f"No Camera {camera_number} known -- run Discover first."
        log(msg)
        emit("error", message=msg)
        return {"success": False, "iterations": [], "final_error_arcmin": None, "message": msg}
    if not platesolver.is_valid_astap_path(astap_path):
        msg = f"ASTAP executable not configured/found: {astap_path!r} -- set it in Settings."
        log(msg)
        emit("error", message=msg)
        return {"success": False, "iterations": [], "final_error_arcmin": None, "message": msg}

    for dev, name in ((telescope, "telescope"), (camera, "camera")):
        if not dev.connected:
            log(f"Connecting {name}...")
            try:
                dev.connected = True
            except AlpacaError as e:
                msg = f"Could not connect {name}: {e}"
                log(msg)
                emit("error", message=msg)
                return {"success": False, "iterations": [], "final_error_arcmin": None,
                        "message": msg}

    ok, reason = check_target_safety(telescope, target_ra_hours, target_dec_deg,
                                      min_altitude_deg, sun_exclusion_deg,
                                      fallback_lat=fallback_lat, fallback_lon=fallback_lon)
    log(f"Safety check: {reason}")
    if not ok:
        emit("rejected", message=reason)
        return {"success": False, "iterations": [], "final_error_arcmin": None, "message": reason}

    out_dir = output_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)), "images")
    os.makedirs(out_dir, exist_ok=True)

    try:
        cansync = bool(telescope.cansync)
    except AlpacaError:
        cansync = False

    iterations = []
    cmd_ra, cmd_dec = target_ra_hours, target_dec_deg

    log(f"\nSlewing to target RA={target_ra_hours:.4f}h Dec={target_dec_deg:.3f}deg ...")
    emit("slewing", target_ra=target_ra_hours, target_dec=target_dec_deg)
    if aborted():
        return {"success": False, "iterations": [], "final_error_arcmin": None, "message": "Aborted."}
    if not _slew_and_wait(telescope, cmd_ra, cmd_dec, log, abort_event=abort_event):
        msg = "Initial slew failed, timed out, or was aborted."
        emit("error", message=msg)
        return {"success": False, "iterations": [], "final_error_arcmin": None, "message": msg}
    log("Initial slew complete.")

    for i in range(1, max_iterations + 1):
        if aborted():
            msg = f"Aborted before iteration {i}."
            log(msg)
            emit("aborted", iteration=i)
            return {"success": False, "iterations": iterations, "final_error_arcmin": None,
                    "message": msg}

        log(f"\n{'=' * 48}\nSLEW & CENTER -- ITERATION {i}/{max_iterations}\n{'=' * 48}\n")
        log(f"Target:\nRA  = {target_ra_hours:.6f} h\nDec = {target_dec_deg:.4f} deg")
        emit("iteration_start", iteration=i, max_iterations=max_iterations,
             target_ra=target_ra_hours, target_dec=target_dec_deg)

        log(f"\nExposing {exposure_seconds}s on {camera.label()} ...")
        try:
            array = imaging.take_exposure(camera, exposure_seconds, light=True)
        except (AlpacaError, imaging.ExposureTimeout) as e:
            msg = f"Exposure failed on iteration {i}: {e}"
            log(msg)
            emit("error", iteration=i, message=msg)
            return {"success": False, "iterations": iterations, "final_error_arcmin": None,
                    "message": msg}

        try:
            reported_ra = telescope.get("rightascension")
            reported_dec = telescope.get("declination")
        except AlpacaError:
            reported_ra = reported_dec = None

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fits_path = os.path.join(out_dir, f"center_iter{i}_{ts}.fits")
        header_items = imaging.build_fits_header_items(
            camera, telescope, exposure_seconds, reported_ra, reported_dec)
        imaging.write_fits(fits_path, array, header_items=header_items)
        log(f"FITS: {fits_path}")

        log("\nASTAP solve ...")
        solve = platesolver.solve_fits(
            fits_path, astap_path, timeout=plate_solve_timeout,
            radius_deg=30 if i == 1 else 10, fov_deg=0, downsample=2,
            ra_hint_hours=reported_ra, dec_hint_deg=reported_dec,
        )
        if not solve["success"] and i == 1:
            # Blind/wide fallback for a cold-boot first slew that may be
            # significantly off -- bounded to one extra attempt, not an
            # unbounded blind search.
            log(f"ASTAP solve FAILED: {solve['message']}")
            log("Retrying with a larger blind search radius...")
            solve = platesolver.solve_fits(
                fits_path, astap_path, timeout=plate_solve_timeout,
                radius_deg=180, fov_deg=0, downsample=4,
            )

        if not solve["success"]:
            log(f"ASTAP solve FAILED: {solve['message']}")
            iterations.append({"iteration": i, "solve": solve})
            msg = f"Plate solve failed on iteration {i}: {solve['message']}"
            emit("solve_failed", iteration=i, message=solve["message"])
            return {"success": False, "iterations": iterations, "final_error_arcmin": None,
                    "message": msg}

        solved_ra, solved_dec = solve["ra_hours"], solve["dec_deg"]
        log(f"\nASTAP solve:\nRA  = {solved_ra:.6f} h\nDec = {solved_dec:.4f} deg")

        error_deg = angular_separation_deg(target_ra_hours, target_dec_deg, solved_ra, solved_dec)
        error_arcmin = error_deg * 60.0
        log(f"\nPointing error:\n{error_deg:.4f} deg\n{error_arcmin:.2f} arcmin "
            f"({error_arcmin * 60.0:.1f} arcsec)")

        record = {"iteration": i, "solve": solve, "error_deg": error_deg,
                   "error_arcmin": error_arcmin}
        iterations.append(record)
        emit("solved", iteration=i, solved_ra=solved_ra, solved_dec=solved_dec,
             error_arcmin=error_arcmin)

        if error_arcmin <= tolerance_arcmin:
            log(f"\nWithin tolerance ({tolerance_arcmin} arcmin) -- CENTERED.")
            emit("centered", iteration=i, error_arcmin=error_arcmin)
            return {"success": True, "iterations": iterations, "final_error_arcmin": error_arcmin,
                    "message": f"Centered within {error_arcmin:.2f} arcmin after {i} iteration(s)."}

        if i == max_iterations:
            msg = (f"Did not reach tolerance after {max_iterations} iteration(s); "
                   f"final error {error_arcmin:.2f} arcmin.")
            log(f"\n{msg}")
            emit("max_iterations", iteration=i, error_arcmin=error_arcmin)
            return {"success": False, "iterations": iterations, "final_error_arcmin": error_arcmin,
                    "message": msg}

        if cansync:
            log(f"\nSyncToCoordinates({solved_ra:.6f}, {solved_dec:.4f}) ...")
            try:
                telescope.sync_to_coordinates(solved_ra, solved_dec)
                log("SyncToCoordinates: ACCEPTED")
                emit("sync", iteration=i, accepted=True)
                # After a successful Sync, the mount's internal model has
                # shifted -- commanding the ORIGINAL target again is now the
                # correct thing to do.
                cmd_ra, cmd_dec = target_ra_hours, target_dec_deg
            except AlpacaError as e:
                log(f"SyncToCoordinates FAILED: ({e.error_number}) {e.error_message} "
                    f"-- falling back to a corrected reslew instead.")
                emit("sync", iteration=i, accepted=False, message=str(e))
                cmd_ra = (cmd_ra + (target_ra_hours - solved_ra)) % 24.0
                cmd_dec = cmd_dec + (target_dec_deg - solved_dec)
        else:
            log("\nCanSync=False -- skipping Sync, using a corrected reslew instead.")
            # No sync model to rely on: shift the COMMANDED position by the
            # measured error so the same offset gets corrected for next time.
            cmd_ra = (cmd_ra + (target_ra_hours - solved_ra)) % 24.0
            cmd_dec = cmd_dec + (target_dec_deg - solved_dec)

        log(f"\nReslewing (commanding RA={cmd_ra:.6f}h Dec={cmd_dec:.4f}deg) ...")
        if aborted():
            msg = f"Aborted after iteration {i}."
            emit("aborted", iteration=i)
            return {"success": False, "iterations": iterations,
                    "final_error_arcmin": error_arcmin, "message": msg}
        if not _slew_and_wait(telescope, cmd_ra, cmd_dec, log, abort_event=abort_event):
            msg = f"Reslew failed, timed out, or was aborted on iteration {i}."
            emit("error", iteration=i, message=msg)
            return {"success": False, "iterations": iterations,
                    "final_error_arcmin": error_arcmin, "message": msg}
        log("Reslew: COMPLETE")

    # Unreachable (the loop always returns), but keeps the function's
    # contract explicit if max_iterations were somehow <= 0.
    return {"success": False, "iterations": iterations, "final_error_arcmin": None,
            "message": "No iterations were run (max_iterations <= 0)."}
