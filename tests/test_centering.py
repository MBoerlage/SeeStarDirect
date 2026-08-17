"""centering.py tests: angular separation math, target safety checks, and
the Slew & Center iteration loop -- all with fake Telescope/Camera objects,
no physical S30 Pro or real ASTAP install required. imaging.write_fits and
imaging.build_fits_header_items run for real (pure local disk I/O, no
hardware) so the FITS-writing integration is still exercised."""

import pytest

import centering
from alpaca.device import AlpacaError


class FakeTelescope:
    def __init__(self, cansync=True, utcdate="2026-08-17T06:00:00Z", lat=30.0, lon=-95.7):
        self._connected = False
        self._cansync = cansync
        self._utcdate = utcdate
        self._lat = lat
        self._lon = lon
        self.slew_calls = []
        self.sync_calls = []
        self.sync_should_fail = False

    def label(self):
        return "Telescope 0"

    @property
    def connected(self):
        return self._connected

    @connected.setter
    def connected(self, value):
        self._connected = bool(value)

    @property
    def cansync(self):
        return self._cansync

    def get(self, prop, **params):
        values = {
            "slewing": False, "utcdate": self._utcdate,
            "sitelatitude": self._lat, "sitelongitude": self._lon,
            "rightascension": 5.5, "declination": -5.0,
        }
        if prop in values:
            return values[prop]
        raise KeyError(prop)

    def slew_to_coordinates_async(self, ra, dec):
        self.slew_calls.append((ra, dec))

    def sync_to_coordinates(self, ra, dec):
        if self.sync_should_fail:
            raise AlpacaError(1035, "sync rejected (test)", "synctocoordinates")
        self.sync_calls.append((ra, dec))


class FakeCamera:
    def __init__(self, device_number=0):
        self._connected = False
        self.device_number = device_number

    def label(self):
        return f"Camera {self.device_number}"

    def display_name(self):
        return "Fake Camera"

    @property
    def connected(self):
        return self._connected

    @connected.setter
    def connected(self, value):
        self._connected = bool(value)


class FakeState:
    def __init__(self, devices):
        self.devices = devices


def make_state(cansync=True, sun_safe_target=True):
    t = FakeTelescope(cansync=cansync)
    c = FakeCamera(0)
    return FakeState({("telescope", 0): t, ("camera", 0): c}), t, c


SAFE_TARGET = (2.5303, 89.2641)  # Polaris -- always high, always far from the Sun


# ---------------------------------------------------------------------------
# angular_separation_deg
# ---------------------------------------------------------------------------

def test_angular_separation_same_point_is_zero():
    assert centering.angular_separation_deg(5.0, 30.0, 5.0, 30.0) == pytest.approx(0.0, abs=1e-9)


def test_angular_separation_one_degree_in_dec_at_equator():
    assert centering.angular_separation_deg(5.0, 0.0, 5.0, 1.0) == pytest.approx(1.0, abs=1e-6)


def test_angular_separation_quarter_sky_in_ra_at_equator():
    assert centering.angular_separation_deg(0.0, 0.0, 6.0, 0.0) == pytest.approx(90.0, abs=1e-6)


def test_angular_separation_is_not_naive_subtraction_near_pole():
    """1 hour of RA near the celestial pole is a tiny angle, NOT 15 degrees
    -- this is exactly the bug 'do not calculate as simple RA/Dec
    subtraction' warns about."""
    sep = centering.angular_separation_deg(0.0, 89.9, 1.0, 89.9)
    assert sep < 1.0  # naive |RA1-RA2|*15 would wrongly claim ~15 deg


def test_angular_separation_symmetric():
    a = centering.angular_separation_deg(3.0, 10.0, 7.0, -20.0)
    b = centering.angular_separation_deg(7.0, -20.0, 3.0, 10.0)
    assert a == pytest.approx(b, abs=1e-9)


# ---------------------------------------------------------------------------
# check_target_safety
# ---------------------------------------------------------------------------

def test_safety_rejects_low_altitude():
    t = FakeTelescope()
    ok, reason = centering.check_target_safety(t, 12.0, -60.0, 20.0, 30.0)
    assert ok is False
    assert "altitude" in reason.lower()


def test_safety_accepts_high_altitude_far_from_sun():
    t = FakeTelescope()
    ok, reason = centering.check_target_safety(t, *SAFE_TARGET, 20.0, 30.0)
    assert ok is True
    assert "altitude=" in reason


def test_safety_rejects_unparseable_utc():
    t = FakeTelescope(utcdate="not-a-date")
    ok, reason = centering.check_target_safety(t, *SAFE_TARGET, 20.0, 30.0)
    assert ok is False
    assert "utcdate" in reason.lower()


def test_safety_surfaces_alpaca_error_reading_state():
    class BrokenTelescope(FakeTelescope):
        def get(self, prop, **params):
            raise AlpacaError(1031, "not connected", prop)

    ok, reason = centering.check_target_safety(BrokenTelescope(), *SAFE_TARGET, 20.0, 30.0)
    assert ok is False


# ---------------------------------------------------------------------------
# slew_and_center -- pre-flight checks (no imaging/platesolver involved)
# ---------------------------------------------------------------------------

def test_slew_and_center_no_telescope():
    state = FakeState({("camera", 0): FakeCamera()})
    result = centering.slew_and_center(state, *SAFE_TARGET, 0, 2.0, 5.0, 3, "fake_astap.exe")
    assert result["success"] is False
    assert "telescope" in result["message"].lower()


def test_slew_and_center_no_camera():
    state = FakeState({("telescope", 0): FakeTelescope()})
    result = centering.slew_and_center(state, *SAFE_TARGET, 0, 2.0, 5.0, 3, "fake_astap.exe")
    assert result["success"] is False
    assert "camera" in result["message"].lower()


def test_slew_and_center_astap_not_configured():
    state, t, c = make_state()
    result = centering.slew_and_center(state, *SAFE_TARGET, 0, 2.0, 5.0, 3, "")
    assert result["success"] is False
    assert "astap" in result["message"].lower()


def test_slew_and_center_rejects_unsafe_target(tmp_path, monkeypatch):
    state, t, c = make_state()
    astap = tmp_path / "astap_cli.exe"
    astap.write_text("")
    result = centering.slew_and_center(
        state, 12.0, -60.0, 0, 2.0, 5.0, 3, str(astap),  # below horizon at this site/time
        output_dir=str(tmp_path))
    assert result["success"] is False
    assert result["iterations"] == []
    assert len(t.slew_calls) == 0  # must reject BEFORE ever slewing


# ---------------------------------------------------------------------------
# slew_and_center -- full loop, imaging/platesolver mocked
# ---------------------------------------------------------------------------

def _patch_exposure(monkeypatch):
    import numpy as np
    monkeypatch.setattr(centering.imaging, "take_exposure",
                         lambda camera, duration, light=True: np.zeros((10, 10), dtype=np.int32))


def test_slew_and_center_converges_first_iteration(tmp_path, monkeypatch):
    state, t, c = make_state()
    astap = tmp_path / "astap_cli.exe"
    astap.write_text("")
    _patch_exposure(monkeypatch)

    target_ra, target_dec = SAFE_TARGET
    monkeypatch.setattr(centering.platesolver, "solve_fits",
                         lambda *a, **k: {"success": True, "ra_hours": target_ra,
                                           "dec_deg": target_dec, "rotation_deg": 0,
                                           "pixel_scale_arcsec": 1.0, "solve_time_seconds": 0.1,
                                           "message": "solved"})

    result = centering.slew_and_center(state, target_ra, target_dec, 0, 2.0, 5.0, 3,
                                        str(astap), output_dir=str(tmp_path))
    assert result["success"] is True
    assert len(result["iterations"]) == 1
    assert result["final_error_arcmin"] == pytest.approx(0.0, abs=1e-6)
    assert len(t.slew_calls) == 1  # only the initial slew -- no reslew needed
    assert len(t.sync_calls) == 0  # centered before Sync would ever be considered


def test_slew_and_center_uses_sync_then_converges(tmp_path, monkeypatch):
    state, t, c = make_state(cansync=True)
    astap = tmp_path / "astap_cli.exe"
    astap.write_text("")
    _patch_exposure(monkeypatch)

    target_ra, target_dec = SAFE_TARGET
    calls = {"n": 0}

    def fake_solve(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            # 2 degrees off in Dec -- well outside a 5 arcmin tolerance.
            # (Offsetting in RA instead would be wrong here: RA-hours near
            # Polaris's near-pole declination correspond to a much smaller
            # true angular separation -- see the near-pole test above.)
            return {"success": True, "ra_hours": target_ra, "dec_deg": target_dec - 2.0,
                     "rotation_deg": 0, "pixel_scale_arcsec": 1.0, "solve_time_seconds": 0.1,
                     "message": "solved"}
        return {"success": True, "ra_hours": target_ra, "dec_deg": target_dec,
                "rotation_deg": 0, "pixel_scale_arcsec": 1.0, "solve_time_seconds": 0.1,
                "message": "solved"}

    monkeypatch.setattr(centering.platesolver, "solve_fits", fake_solve)

    result = centering.slew_and_center(state, target_ra, target_dec, 0, 2.0, 5.0, 3,
                                        str(astap), output_dir=str(tmp_path))
    assert result["success"] is True
    assert len(result["iterations"]) == 2
    assert len(t.sync_calls) == 1
    assert len(t.slew_calls) == 2  # initial + one reslew
    # After a successful Sync, the reslew commands the ORIGINAL target again.
    assert t.slew_calls[1] == pytest.approx((target_ra, target_dec), abs=1e-6)


def test_slew_and_center_no_sync_fallback_applies_correction(tmp_path, monkeypatch):
    state, t, c = make_state(cansync=False)
    astap = tmp_path / "astap_cli.exe"
    astap.write_text("")
    _patch_exposure(monkeypatch)

    target_ra, target_dec = SAFE_TARGET
    offset_dec = 1.0  # 1 degree of Dec -- a real 1-degree separation
    calls = {"n": 0}

    def fake_solve(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"success": True, "ra_hours": target_ra, "dec_deg": target_dec - offset_dec,
                     "rotation_deg": 0, "pixel_scale_arcsec": 1.0, "solve_time_seconds": 0.1,
                     "message": "solved"}
        return {"success": True, "ra_hours": target_ra, "dec_deg": target_dec,
                "rotation_deg": 0, "pixel_scale_arcsec": 1.0, "solve_time_seconds": 0.1,
                "message": "solved"}

    monkeypatch.setattr(centering.platesolver, "solve_fits", fake_solve)

    result = centering.slew_and_center(state, target_ra, target_dec, 0, 2.0, 5.0, 3,
                                        str(astap), output_dir=str(tmp_path))
    assert result["success"] is True
    assert len(t.sync_calls) == 0  # CanSync=False -- must never be called
    assert len(t.slew_calls) == 2
    # No-sync fallback must NOT just repeat the same original target -- that
    # would never converge. It should command target + (target - solved).
    corrected_dec = t.slew_calls[1][1]
    assert corrected_dec != pytest.approx(target_dec, abs=1e-6)
    assert corrected_dec == pytest.approx(target_dec + offset_dec, abs=1e-6)


def test_slew_and_center_sync_failure_falls_back_to_corrected_reslew(tmp_path, monkeypatch):
    """Even with CanSync=True, if the actual Sync call is rejected at
    runtime, the loop must not just give up -- it should fall back to the
    same corrected-reslew math as the no-sync case."""
    state, t, c = make_state(cansync=True)
    t.sync_should_fail = True
    astap = tmp_path / "astap_cli.exe"
    astap.write_text("")
    _patch_exposure(monkeypatch)

    target_ra, target_dec = SAFE_TARGET
    calls = {"n": 0}

    def fake_solve(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"success": True, "ra_hours": target_ra, "dec_deg": target_dec - 1.0,
                     "rotation_deg": 0, "pixel_scale_arcsec": 1.0, "solve_time_seconds": 0.1,
                     "message": "solved"}
        return {"success": True, "ra_hours": target_ra, "dec_deg": target_dec,
                "rotation_deg": 0, "pixel_scale_arcsec": 1.0, "solve_time_seconds": 0.1,
                "message": "solved"}

    monkeypatch.setattr(centering.platesolver, "solve_fits", fake_solve)

    result = centering.slew_and_center(state, target_ra, target_dec, 0, 2.0, 5.0, 3,
                                        str(astap), output_dir=str(tmp_path))
    assert result["success"] is True
    assert len(t.sync_calls) == 0  # the Sync attempt failed, so it must not "count"
    assert t.slew_calls[1][1] != pytest.approx(target_dec, abs=1e-6)


def test_slew_and_center_solve_failure_returns_failure(tmp_path, monkeypatch):
    state, t, c = make_state()
    astap = tmp_path / "astap_cli.exe"
    astap.write_text("")
    _patch_exposure(monkeypatch)

    monkeypatch.setattr(centering.platesolver, "solve_fits",
                         lambda *a, **k: {"success": False, "message": "Not enough stars.",
                                           "ra_hours": None, "dec_deg": None, "rotation_deg": None,
                                           "pixel_scale_arcsec": None, "solve_time_seconds": 0.1})

    result = centering.slew_and_center(state, *SAFE_TARGET, 0, 2.0, 5.0, 3,
                                        str(astap), output_dir=str(tmp_path))
    assert result["success"] is False
    assert "solve failed" in result["message"].lower()
    assert len(result["iterations"]) == 1


def test_slew_and_center_max_iterations_reached(tmp_path, monkeypatch):
    state, t, c = make_state()
    astap = tmp_path / "astap_cli.exe"
    astap.write_text("")
    _patch_exposure(monkeypatch)

    target_ra, target_dec = SAFE_TARGET
    # Always half a degree off -- outside a 5 arcmin tolerance, forever.
    monkeypatch.setattr(centering.platesolver, "solve_fits",
                         lambda *a, **k: {"success": True, "ra_hours": target_ra,
                                           "dec_deg": target_dec + 0.5, "rotation_deg": 0,
                                           "pixel_scale_arcsec": 1.0, "solve_time_seconds": 0.1,
                                           "message": "solved"})

    result = centering.slew_and_center(state, target_ra, target_dec, 0, 2.0, 5.0, 2,
                                        str(astap), output_dir=str(tmp_path))
    assert result["success"] is False
    assert len(result["iterations"]) == 2
    assert result["final_error_arcmin"] == pytest.approx(30.0, abs=0.5)
    assert "did not reach tolerance" in result["message"].lower()


def test_slew_and_center_aborts_before_first_iteration(tmp_path, monkeypatch):
    import threading
    state, t, c = make_state()
    astap = tmp_path / "astap_cli.exe"
    astap.write_text("")
    _patch_exposure(monkeypatch)
    monkeypatch.setattr(centering.platesolver, "solve_fits",
                         lambda *a, **k: pytest.fail("solve_fits should not be called after abort"))

    ev = threading.Event()
    ev.set()  # already aborted before the run even starts
    result = centering.slew_and_center(state, *SAFE_TARGET, 0, 2.0, 5.0, 3,
                                        str(astap), output_dir=str(tmp_path), abort_event=ev)
    assert result["success"] is False
    assert "abort" in result["message"].lower()


def test_slew_and_center_connects_devices_automatically(tmp_path, monkeypatch):
    state, t, c = make_state()
    assert t.connected is False and c.connected is False
    astap = tmp_path / "astap_cli.exe"
    astap.write_text("")
    _patch_exposure(monkeypatch)
    target_ra, target_dec = SAFE_TARGET
    monkeypatch.setattr(centering.platesolver, "solve_fits",
                         lambda *a, **k: {"success": True, "ra_hours": target_ra,
                                           "dec_deg": target_dec, "rotation_deg": 0,
                                           "pixel_scale_arcsec": 1.0, "solve_time_seconds": 0.1,
                                           "message": "solved"})
    centering.slew_and_center(state, target_ra, target_dec, 0, 2.0, 5.0, 3,
                               str(astap), output_dir=str(tmp_path))
    assert t.connected is True
    assert c.connected is True
