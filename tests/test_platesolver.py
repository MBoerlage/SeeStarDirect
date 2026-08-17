"""platesolver.py tests. No physical ASTAP install or hardware required --
subprocess and filesystem calls are mocked. The .ini parsing format tested
here is the REAL format confirmed by running astap_cli.exe live against a
synthetic unsolvable FITS file during development (see platesolver.py's
module docstring)."""

import subprocess
from types import SimpleNamespace

import numpy as np
import pytest

import imaging
import platesolver


# ---------------------------------------------------------------------------
# find_astap / is_valid_astap_path
# ---------------------------------------------------------------------------

def test_find_astap_checks_path_first(monkeypatch):
    monkeypatch.setattr(platesolver.shutil, "which",
                         lambda name: r"C:\somewhere\astap_cli.exe" if name == "astap_cli" else None)
    assert platesolver.find_astap() == r"C:\somewhere\astap_cli.exe"


def test_find_astap_falls_back_to_common_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(platesolver.shutil, "which", lambda name: None)
    fake_path = tmp_path / "astap_cli.exe"
    fake_path.write_text("")
    monkeypatch.setattr(platesolver, "COMMON_WINDOWS_PATHS", [str(fake_path)])
    assert platesolver.find_astap() == str(fake_path)


def test_find_astap_returns_none_when_nothing_found(monkeypatch):
    monkeypatch.setattr(platesolver.shutil, "which", lambda name: None)
    monkeypatch.setattr(platesolver, "COMMON_WINDOWS_PATHS", [r"C:\nowhere\astap.exe"])
    assert platesolver.find_astap() is None


def test_is_valid_astap_path_rejects_blank_and_missing(tmp_path):
    assert platesolver.is_valid_astap_path("") is False
    assert platesolver.is_valid_astap_path(None) is False
    assert platesolver.is_valid_astap_path(str(tmp_path / "nope.exe")) is False


def test_is_valid_astap_path_accepts_real_file(tmp_path):
    f = tmp_path / "astap_cli.exe"
    f.write_text("")
    assert platesolver.is_valid_astap_path(str(f)) is True


# ---------------------------------------------------------------------------
# test_astap
# ---------------------------------------------------------------------------

def test_test_astap_parses_version_from_real_help_text(monkeypatch, tmp_path):
    """This is the ACTUAL first line astap_cli.exe -h prints, captured live
    2026-08-17 -- not a guess."""
    f = tmp_path / "astap_cli.exe"
    f.write_text("")
    real_output = ("ASTAP astrometric solver version CLI-2026.07.16\n"
                   "(C) 2018, 2025 by Han Kleijn. License MPL 2.0\n")
    monkeypatch.setattr(platesolver.subprocess, "run",
                         lambda *a, **k: SimpleNamespace(stdout=real_output, stderr="", returncode=0))
    result = platesolver.test_astap(str(f))
    assert result["ok"] is True
    assert result["version"] == "CLI-2026.07.16"


def test_test_astap_missing_file():
    result = platesolver.test_astap("does_not_exist.exe")
    assert result["ok"] is False


def test_test_astap_timeout(monkeypatch, tmp_path):
    f = tmp_path / "astap_cli.exe"
    f.write_text("")

    def raise_timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="astap_cli", timeout=8)

    monkeypatch.setattr(platesolver.subprocess, "run", raise_timeout)
    result = platesolver.test_astap(str(f))
    assert result["ok"] is False
    assert "Timed out" in result["message"]


# ---------------------------------------------------------------------------
# solve_fits -- failure path is the one confirmed live against real ASTAP;
# success path is grounded in documented -update/WCS behavior via a
# synthetic solved FITS header.
# ---------------------------------------------------------------------------

@pytest.fixture
def fits_file(tmp_path):
    path = tmp_path / "test.fits"
    arr = (np.random.rand(50, 60) * 1000).astype(np.int32)
    imaging.write_fits(str(path), arr)
    return path


def test_solve_fits_missing_astap(fits_file):
    result = platesolver.solve_fits(str(fits_file), "does_not_exist.exe")
    assert result["success"] is False
    assert "not found" in result["message"]


def test_solve_fits_missing_input_file(tmp_path):
    astap = tmp_path / "astap_cli.exe"
    astap.write_text("")
    result = platesolver.solve_fits(str(tmp_path / "nope.fits"), str(astap))
    assert result["success"] is False
    assert "not found" in result["message"]


def test_solve_fits_failure_matches_real_ini_format(monkeypatch, fits_file, tmp_path):
    """Real .ini content captured live from astap_cli.exe on an unsolvable
    synthetic image, 2026-08-17:
        PLTSOLVD=F
        CMDLINE="..."
        ERROR=Not enough stars.
        WARNING=Warning, remaining image dimensions too low!
    """
    astap = tmp_path / "astap_cli.exe"
    astap.write_text("")

    ini_path = fits_file.with_suffix(".ini")
    ini_path.write_text(
        'PLTSOLVD=F\n'
        'CMDLINE="fake"\n'
        'ERROR=Not enough stars.\n'
        'WARNING=Warning, remaining image dimensions too low!\n'
    )
    monkeypatch.setattr(platesolver.subprocess, "run",
                         lambda *a, **k: SimpleNamespace(stdout="Only 0 stars found. Abort",
                                                          stderr="", returncode=0))
    result = platesolver.solve_fits(str(fits_file), str(astap))
    assert result["success"] is False
    assert "Not enough stars" in result["message"]
    assert result["ra_hours"] is None


def test_solve_fits_success_reads_wcs_from_updated_header(monkeypatch, fits_file, tmp_path):
    """Per ASTAP's own -h text: '-update: Add the solution to the input
    fits file header.' Simulates that by patching CRVAL1/CRVAL2/etc into
    the FITS header ourselves (astropy round-trip), since no real solvable
    star field was available during development -- see platesolver.py's
    module docstring for what's live-verified vs. grounded-but-untested."""
    astap = tmp_path / "astap_cli.exe"
    astap.write_text("")

    from astropy.io import fits as pyfits
    with pyfits.open(str(fits_file), mode="update") as hdul:
        hdul[0].header["PLTSOLVD"] = "T"
        hdul[0].header["CRVAL1"] = 84.9  # deg -> 5.66 h
        hdul[0].header["CRVAL2"] = -5.4
        hdul[0].header["CROTA2"] = 1.5
        hdul[0].header["CDELT2"] = 0.0005  # deg/px -> 1.8 arcsec/px
        hdul.flush()

    ini_path = fits_file.with_suffix(".ini")
    ini_path.write_text('PLTSOLVD=T\nCMDLINE="fake"\n')

    monkeypatch.setattr(platesolver.subprocess, "run",
                         lambda *a, **k: SimpleNamespace(stdout="Solution found!", stderr="",
                                                          returncode=0))
    result = platesolver.solve_fits(str(fits_file), str(astap))
    assert result["success"] is True
    assert result["ra_hours"] == pytest.approx(84.9 / 15.0, abs=1e-6)
    assert result["dec_deg"] == pytest.approx(-5.4, abs=1e-6)
    assert result["rotation_deg"] == pytest.approx(1.5, abs=1e-6)
    assert result["pixel_scale_arcsec"] == pytest.approx(1.8, abs=1e-3)


def test_solve_fits_exit_code_alone_is_not_trusted(monkeypatch, fits_file, tmp_path):
    """Confirmed live: astap_cli.exe exits 0 even when it finds zero stars.
    If there's no .ini and no PLTSOLVD in the header, exit 0 must NOT be
    treated as success."""
    astap = tmp_path / "astap_cli.exe"
    astap.write_text("")
    # No .ini written, header has no PLTSOLVD -- exactly the ambiguous case.
    monkeypatch.setattr(platesolver.subprocess, "run",
                         lambda *a, **k: SimpleNamespace(stdout="", stderr="", returncode=0))
    result = platesolver.solve_fits(str(fits_file), str(astap))
    assert result["success"] is False


def test_solve_fits_timeout(monkeypatch, fits_file, tmp_path):
    astap = tmp_path / "astap_cli.exe"
    astap.write_text("")

    def raise_timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="astap_cli", timeout=60)

    monkeypatch.setattr(platesolver.subprocess, "run", raise_timeout)
    result = platesolver.solve_fits(str(fits_file), str(astap), timeout=60)
    assert result["success"] is False
    assert "timed out" in result["message"].lower()
