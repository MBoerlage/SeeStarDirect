"""
Exposure workflow + local image saving, using ONLY the native Alpaca Camera
interface (StartExposure / ImageReady / ImageArray). No bridge, no HTTP URL
handed back by a third party -- the pixel data comes straight from the
camera's ImageArray response.

Both S30 Pro cameras report SensorType=2 (RGGB Bayer), so ImageArray is a
2D array of RAW mosaic values, not debayered RGB. FITS gets the raw values
untouched (that's the astronomically correct thing to keep). The PNG is a
quick, lossy, half-resolution debayered preview only -- not a substitute for
real processing.
"""

import logging
import time

import numpy as np
from PIL import Image

logger = logging.getLogger("imaging")


class ExposureTimeout(Exception):
    pass


def take_exposure(camera, duration_s, light=True, poll_interval=0.5, timeout_margin=30):
    """Runs StartExposure -> poll ImageReady -> ImageArray. Returns the raw
    Value from ImageArray (nested Python lists, as returned by Alpaca)."""
    camera.start_exposure(duration_s, light=light)
    deadline = time.time() + duration_s + timeout_margin
    while time.time() < deadline:
        if camera.get("imageready"):
            break
        time.sleep(poll_interval)
    else:
        raise ExposureTimeout(
            f"Timed out waiting for ImageReady after {duration_s + timeout_margin:.0f}s"
        )
    logger.info("Exposure ready, downloading ImageArray ...")
    return camera.get_image_array(timeout=60)


# ---------------------------------------------------------------------------
# FITS (minimal, pure-Python/numpy writer -- no astropy dependency)
# ---------------------------------------------------------------------------

def _card(keyword, value, comment=""):
    if isinstance(value, bool):
        vs = ("T" if value else "F").rjust(20)
    elif isinstance(value, int):
        vs = str(value).rjust(20)
    elif isinstance(value, float):
        vs = f"{value:.10g}".rjust(20)
    else:
        s = str(value).replace("'", "''")
        vs = f"'{s}'".ljust(20) if len(s) >= 8 else f"'{s:<8}'".ljust(20)
    line = f"{keyword:<8}= {vs}"
    if comment:
        line += f" / {comment}"
    return line[:80].ljust(80)


def write_fits(path, array, header_items=None):
    """Writes a minimal, spec-compliant FITS file with RAW pixel values
    preserved exactly (int32 or float32, no BSCALE/BZERO games needed since
    the S30 Pro's 16-bit-range ADU values fit natively in signed 32-bit)."""
    arr = np.asarray(array)

    if arr.dtype.kind == "f":
        bitpix = -32
        data = arr.astype(">f4")
    else:
        bitpix = 32
        data = arr.astype(">i4")

    if data.ndim == 2:
        h, w = data.shape
        naxis_cards = [("NAXIS", 2), ("NAXIS1", w), ("NAXIS2", h)]
    elif data.ndim == 3:
        h, w, planes = data.shape
        # FITS stores fastest-varying axis first; move the color/plane axis
        # to the front so NAXIS1/2/3 read as (width, height, planes).
        data = np.ascontiguousarray(np.moveaxis(data, 2, 0))
        naxis_cards = [("NAXIS", 3), ("NAXIS1", w), ("NAXIS2", h), ("NAXIS3", planes)]
    else:
        raise ValueError(f"Unsupported array rank {data.ndim} for FITS output")

    cards = [
        _card("SIMPLE", True, "conforms to FITS standard"),
        _card("BITPIX", bitpix),
    ]
    for k, v in naxis_cards:
        cards.append(_card(k, v))
    cards.append(_card("DATE", time.strftime("%Y-%m-%dT%H:%M:%S"), "file write time, local"))
    for item in header_items or []:
        k, v, c = item if len(item) == 3 else (item[0], item[1], "")
        cards.append(_card(k, v, c))
    cards.append("END".ljust(80))

    header_bytes = "".join(cards).encode("ascii")
    header_bytes += b" " * ((2880 - len(header_bytes) % 2880) % 2880)

    data_bytes = data.tobytes()
    data_bytes += b"\x00" * ((2880 - len(data_bytes) % 2880) % 2880)

    with open(path, "wb") as f:
        f.write(header_bytes)
        f.write(data_bytes)


def _format_ra_sexagesimal(hours):
    h = int(hours)
    m_full = (hours - h) * 60
    m = int(m_full)
    s = (m_full - m) * 60
    return f"{h:02d} {m:02d} {s:05.2f}"


def _format_dec_sexagesimal(deg):
    sign = "-" if deg < 0 else "+"
    deg = abs(deg)
    d = int(deg)
    m_full = (deg - d) * 60
    m = int(m_full)
    s = (m_full - m) * 60
    return f"{sign}{d:02d} {m:02d} {s:04.1f}"


def build_fits_header_items(camera, telescope=None, exposure_seconds=None,
                             ra_hours=None, dec_deg=None):
    """Builds the (key, value, comment) list write_fits() expects, from
    whatever real values are actually available -- never invents a value
    for something that couldn't be read. `telescope` and ra_hours/dec_deg
    are optional (e.g. a bare capability check has no telescope handy)."""
    items = []
    if exposure_seconds is not None:
        items.append(("EXPTIME", float(exposure_seconds), "exposure time in seconds"))
    items.append(("DATE-OBS", time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
                  "UTC date/time of file write (approx)"))
    try:
        items.append(("INSTRUME", camera.display_name(), "camera device name"))
    except Exception:
        pass
    items.append(("TELESCOP", "Seestar S30 Pro", "telescope model"))

    if ra_hours is not None:
        items.append(("RA", float(ra_hours) * 15.0, "telescope-reported RA, deg"))
        items.append(("OBJCTRA", _format_ra_sexagesimal(float(ra_hours)),
                      "telescope-reported RA, sexagesimal"))
    if dec_deg is not None:
        items.append(("DEC", float(dec_deg), "telescope-reported Dec, deg"))
        items.append(("OBJCTDEC", _format_dec_sexagesimal(float(dec_deg)),
                      "telescope-reported Dec, sexagesimal"))

    if telescope is not None:
        try:
            items.append(("SITELAT", float(telescope.get("sitelatitude")), "site latitude, deg"))
        except Exception:
            pass
        try:
            items.append(("SITELONG", float(telescope.get("sitelongitude")), "site longitude, deg"))
        except Exception:
            pass

    return items


# ---------------------------------------------------------------------------
# PNG preview (quick look only)
# ---------------------------------------------------------------------------

# ASCOM SensorType enum values that mean "single-plane Bayer mosaic".
BAYER_SENSOR_TYPES = {2, 3, 4}  # RGGB, CMYG, CMYG2


def _auto_scale_to_uint8(arr):
    arr = arr.astype(np.float64)
    lo, hi = np.percentile(arr, [0.5, 99.5])
    if hi <= lo:
        hi = lo + 1.0
    scaled = np.clip((arr - lo) / (hi - lo) * 255.0, 0, 255)
    return scaled.astype(np.uint8)


def _debayer_rggb_quick(raw):
    """Fast, low-quality 2x2-block debayer (half resolution). Good enough
    for a quick preview; NOT suitable for real astrophotography processing."""
    h, w = raw.shape
    h2, w2 = h // 2, w // 2
    raw = raw[: h2 * 2, : w2 * 2].astype(np.float64)
    r = raw[0::2, 0::2]
    g = (raw[0::2, 1::2] + raw[1::2, 0::2]) / 2.0
    b = raw[1::2, 1::2]
    return np.stack([r, g, b], axis=-1)


def save_preview_png(path, array, sensor_type=None):
    arr = np.asarray(array)

    if arr.ndim == 2:
        if sensor_type in BAYER_SENSOR_TYPES:
            rgb = _debayer_rggb_quick(arr)
            img = Image.fromarray(_auto_scale_to_uint8(rgb), mode="RGB")
        else:
            img = Image.fromarray(_auto_scale_to_uint8(arr), mode="L")
    elif arr.ndim == 3:
        img = Image.fromarray(_auto_scale_to_uint8(arr), mode="RGB")
    else:
        raise ValueError(f"Unsupported array rank {arr.ndim} for PNG output")

    img.save(path)
