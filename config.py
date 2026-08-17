"""Persistent configuration (config.json next to this file).

The Alpaca port is intentionally NOT stored persistently -- discovery is
supposed to determine it each run, per the Alpaca spec. preferred_server_ip
/ preferred_server_port are only a manual fallback for when discovery fails
(e.g. broadcast blocked by network config).

validate_settings() is deliberately Tkinter-free and pure-Python so it can
be unit tested without a display and reused identically by gui.py's Save
Settings handler and by the console app.
"""

import json
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")

DEFAULT_CONFIG = {
    "client_id": 1234,
    "discovery_timeout": 3,
    "preferred_server_ip": None,
    "preferred_server_port": None,
    "output_directory": "images",
    # Plate solving
    "astap_path": "",
    "plate_solve_timeout": 60,
    # Slew & Center defaults
    "centering_tolerance_arcmin": 5.0,
    "centering_max_iterations": 3,
    "centering_exposure_seconds": 2.0,
    "centering_camera": 0,
    "minimum_target_altitude_deg": 20.0,
    "sun_exclusion_deg": 30.0,
    # Observing site fallback -- only used by safety checks (altitude/Sun
    # exclusion) if the telescope's own SiteLatitude/SiteLongitude can't be
    # read. The telescope's live values are always preferred when available.
    "fallback_latitude": None,
    "fallback_longitude": None,
}


def load_config(path=CONFIG_PATH):
    """Old config.json files missing the newer keys above still load fine
    -- DEFAULT_CONFIG is merged first, so anything not present on disk just
    keeps its default."""
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                cfg.update(json.load(f))
        except (json.JSONDecodeError, OSError):
            print(f"[!] Could not read {path}, using defaults.")
    return cfg


def save_config(cfg, path=CONFIG_PATH):
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)


def _to_int(key, value, errors, min_value=None):
    if value is None or value == "":
        errors.append(f"{key}: required")
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        errors.append(f"{key}: must be an integer, got {value!r}")
        return None
    if min_value is not None and n < min_value:
        errors.append(f"{key}: must be >= {min_value}, got {n}")
        return None
    return n


def _to_float(key, value, errors, min_value=None, max_value=None, exclusive_min=False):
    if value is None or value == "":
        errors.append(f"{key}: required")
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        errors.append(f"{key}: must be a number, got {value!r}")
        return None
    if min_value is not None:
        if exclusive_min and n <= min_value:
            errors.append(f"{key}: must be > {min_value}, got {n}")
            return None
        if not exclusive_min and n < min_value:
            errors.append(f"{key}: must be >= {min_value}, got {n}")
            return None
    if max_value is not None and n > max_value:
        errors.append(f"{key}: must be <= {max_value}, got {n}")
        return None
    return n


def validate_settings(raw):
    """raw: dict of setting_name -> value, typically raw strings straight
    out of GUI Entry widgets (but already-correct-typed values work too --
    only keys actually present in `raw` are validated/returned). Only
    non-blank/blank-allowed rules from the project spec are enforced here.

    Returns (ok: bool, errors: list[str], parsed: dict). If ok is False,
    `parsed` should NOT be saved -- validate_settings never partially
    applies a bad batch."""
    errors = []
    parsed = {}

    if "client_id" in raw:
        v = _to_int("client_id", raw["client_id"], errors, min_value=0)
        if v is not None:
            parsed["client_id"] = v

    if "discovery_timeout" in raw:
        v = _to_float("discovery_timeout", raw["discovery_timeout"], errors,
                       min_value=0, exclusive_min=True)
        if v is not None:
            parsed["discovery_timeout"] = v

    if "preferred_server_ip" in raw:
        val = raw["preferred_server_ip"]
        val = val.strip() if isinstance(val, str) else val
        parsed["preferred_server_ip"] = val or None

    if "preferred_server_port" in raw:
        val = raw["preferred_server_port"]
        val = val.strip() if isinstance(val, str) else val
        if val in (None, ""):
            parsed["preferred_server_port"] = None
        else:
            try:
                port = int(val)
            except (TypeError, ValueError):
                errors.append(f"preferred_server_port: must be blank or an integer, got {val!r}")
            else:
                if not (1 <= port <= 65535):
                    errors.append(
                        f"preferred_server_port: must be a valid TCP port (1-65535), got {port}")
                else:
                    parsed["preferred_server_port"] = port

    if "output_directory" in raw:
        val = raw["output_directory"]
        val = val.strip() if isinstance(val, str) else val
        if not val:
            errors.append("output_directory: required")
        else:
            parsed["output_directory"] = val

    if "astap_path" in raw:
        # Blank is allowed (means "not configured yet"); existence is
        # checked at USE time (platesolver.is_valid_astap_path), not here --
        # the user may be typing a path before installing ASTAP.
        val = raw["astap_path"]
        parsed["astap_path"] = val.strip() if isinstance(val, str) else (val or "")

    if "plate_solve_timeout" in raw:
        v = _to_float("plate_solve_timeout", raw["plate_solve_timeout"], errors,
                       min_value=0, exclusive_min=True)
        if v is not None:
            parsed["plate_solve_timeout"] = v

    if "centering_tolerance_arcmin" in raw:
        v = _to_float("centering_tolerance_arcmin", raw["centering_tolerance_arcmin"], errors,
                       min_value=0, exclusive_min=True)
        if v is not None:
            parsed["centering_tolerance_arcmin"] = v

    if "centering_max_iterations" in raw:
        v = _to_int("centering_max_iterations", raw["centering_max_iterations"], errors,
                     min_value=1)
        if v is not None:
            parsed["centering_max_iterations"] = v

    if "centering_exposure_seconds" in raw:
        v = _to_float("centering_exposure_seconds", raw["centering_exposure_seconds"], errors,
                       min_value=0, exclusive_min=True)
        if v is not None:
            parsed["centering_exposure_seconds"] = v

    if "centering_camera" in raw:
        v = _to_int("centering_camera", raw["centering_camera"], errors, min_value=0)
        if v is not None:
            parsed["centering_camera"] = v

    if "minimum_target_altitude_deg" in raw:
        v = _to_float("minimum_target_altitude_deg", raw["minimum_target_altitude_deg"], errors,
                       min_value=-90, max_value=90)
        if v is not None:
            parsed["minimum_target_altitude_deg"] = v

    if "sun_exclusion_deg" in raw:
        v = _to_float("sun_exclusion_deg", raw["sun_exclusion_deg"], errors,
                       min_value=0, max_value=180)
        if v is not None:
            parsed["sun_exclusion_deg"] = v

    if "fallback_latitude" in raw:
        val = raw["fallback_latitude"]
        val = val.strip() if isinstance(val, str) else val
        if val in (None, ""):
            parsed["fallback_latitude"] = None
        else:
            v = _to_float("fallback_latitude", val, errors, min_value=-90, max_value=90)
            if v is not None:
                parsed["fallback_latitude"] = v

    if "fallback_longitude" in raw:
        val = raw["fallback_longitude"]
        val = val.strip() if isinstance(val, str) else val
        if val in (None, ""):
            parsed["fallback_longitude"] = None
        else:
            v = _to_float("fallback_longitude", val, errors, min_value=-180, max_value=180)
            if v is not None:
                parsed["fallback_longitude"] = v

    return (len(errors) == 0, errors, parsed)
