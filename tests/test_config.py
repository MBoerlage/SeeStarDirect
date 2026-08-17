"""Config backward compatibility + validation. No hardware/network needed."""

import json

import config


def test_load_config_missing_file_returns_defaults(tmp_path):
    cfg = config.load_config(str(tmp_path / "does_not_exist.json"))
    assert cfg == config.DEFAULT_CONFIG
    assert cfg is not config.DEFAULT_CONFIG  # must be a copy, not the shared dict


def test_load_config_old_style_file_gets_new_keys_from_defaults(tmp_path):
    """An old config.json saved before astap_path/centering_* existed must
    still load fine, with the new keys silently filled from DEFAULT_CONFIG."""
    old_style = {
        "client_id": 42,
        "discovery_timeout": 5,
        "preferred_server_ip": None,
        "preferred_server_port": None,
        "output_directory": "my_images",
    }
    path = tmp_path / "old_config.json"
    path.write_text(json.dumps(old_style))

    cfg = config.load_config(str(path))
    assert cfg["client_id"] == 42
    assert cfg["output_directory"] == "my_images"
    # New keys present with their defaults, not KeyError
    assert cfg["astap_path"] == config.DEFAULT_CONFIG["astap_path"]
    assert cfg["centering_camera"] == config.DEFAULT_CONFIG["centering_camera"]
    assert cfg["sun_exclusion_deg"] == config.DEFAULT_CONFIG["sun_exclusion_deg"]


def test_load_config_corrupt_json_falls_back_to_defaults(tmp_path, capsys):
    path = tmp_path / "corrupt.json"
    path.write_text("{not valid json")
    cfg = config.load_config(str(path))
    assert cfg == config.DEFAULT_CONFIG


def test_save_then_load_roundtrip(tmp_path):
    path = tmp_path / "roundtrip.json"
    cfg = dict(config.DEFAULT_CONFIG)
    cfg["astap_path"] = r"C:\Program Files\astap\astap_cli.exe"
    cfg["centering_tolerance_arcmin"] = 2.5
    config.save_config(cfg, str(path))

    loaded = config.load_config(str(path))
    assert loaded["astap_path"] == cfg["astap_path"]
    assert loaded["centering_tolerance_arcmin"] == 2.5


# ---------------------------------------------------------------------------
# validate_settings
# ---------------------------------------------------------------------------

def test_validate_settings_accepts_good_values():
    ok, errors, parsed = config.validate_settings({
        "client_id": "1234",
        "discovery_timeout": "3",
        "preferred_server_ip": "  192.168.1.101  ",
        "preferred_server_port": "32323",
        "output_directory": "images",
        "astap_path": r"C:\Program Files\astap\astap_cli.exe",
        "plate_solve_timeout": "60",
        "centering_tolerance_arcmin": "5.0",
        "centering_max_iterations": "3",
        "centering_exposure_seconds": "2.0",
        "centering_camera": "0",
        "minimum_target_altitude_deg": "20",
        "sun_exclusion_deg": "30",
    })
    assert ok
    assert errors == []
    assert parsed["client_id"] == 1234
    assert parsed["preferred_server_ip"] == "192.168.1.101"
    assert parsed["preferred_server_port"] == 32323
    assert parsed["centering_max_iterations"] == 3


def test_validate_settings_blank_preferred_ip_and_port_are_none():
    ok, errors, parsed = config.validate_settings({
        "preferred_server_ip": "", "preferred_server_port": "",
    })
    assert ok
    assert parsed["preferred_server_ip"] is None
    assert parsed["preferred_server_port"] is None


def test_validate_settings_rejects_non_integer_client_id():
    ok, errors, parsed = config.validate_settings({"client_id": "abc"})
    assert not ok
    assert any("client_id" in e for e in errors)
    assert "client_id" not in parsed


def test_validate_settings_rejects_negative_client_id():
    ok, errors, parsed = config.validate_settings({"client_id": "-1"})
    assert not ok


def test_validate_settings_rejects_zero_discovery_timeout():
    ok, errors, parsed = config.validate_settings({"discovery_timeout": "0"})
    assert not ok


def test_validate_settings_rejects_out_of_range_port():
    ok, errors, parsed = config.validate_settings({"preferred_server_port": "99999"})
    assert not ok
    assert any("port" in e for e in errors)


def test_validate_settings_rejects_bad_port_string():
    ok, errors, parsed = config.validate_settings({"preferred_server_port": "not-a-port"})
    assert not ok


def test_validate_settings_rejects_empty_output_directory():
    ok, errors, parsed = config.validate_settings({"output_directory": "   "})
    assert not ok


def test_validate_settings_allows_blank_astap_path():
    """Blank astap_path is valid at SAVE time -- existence is checked at
    USE time via platesolver.is_valid_astap_path, not here."""
    ok, errors, parsed = config.validate_settings({"astap_path": ""})
    assert ok
    assert parsed["astap_path"] == ""


def test_validate_settings_rejects_zero_tolerance():
    ok, errors, parsed = config.validate_settings({"centering_tolerance_arcmin": "0"})
    assert not ok


def test_validate_settings_rejects_zero_iterations():
    ok, errors, parsed = config.validate_settings({"centering_max_iterations": "0"})
    assert not ok


def test_validate_settings_rejects_zero_exposure():
    ok, errors, parsed = config.validate_settings({"centering_exposure_seconds": "0"})
    assert not ok


def test_validate_settings_rejects_negative_camera_number():
    ok, errors, parsed = config.validate_settings({"centering_camera": "-1"})
    assert not ok


def test_validate_settings_altitude_range():
    ok, _, _ = config.validate_settings({"minimum_target_altitude_deg": "95"})
    assert not ok
    ok, _, _ = config.validate_settings({"minimum_target_altitude_deg": "-95"})
    assert not ok
    ok, _, parsed = config.validate_settings({"minimum_target_altitude_deg": "20"})
    assert ok
    assert parsed["minimum_target_altitude_deg"] == 20.0


def test_validate_settings_sun_exclusion_range():
    ok, _, _ = config.validate_settings({"sun_exclusion_deg": "181"})
    assert not ok
    ok, _, _ = config.validate_settings({"sun_exclusion_deg": "-1"})
    assert not ok
    ok, _, parsed = config.validate_settings({"sun_exclusion_deg": "30"})
    assert ok


def test_validate_settings_never_partially_applies_a_bad_batch():
    """One bad field among several good ones must reject the WHOLE batch,
    with parsed not containing values that shouldn't be trusted/saved."""
    ok, errors, parsed = config.validate_settings({
        "client_id": "1234",           # good
        "discovery_timeout": "-1",     # bad
    })
    assert not ok
    assert len(errors) == 1
    # The good field is still parsed (caller is expected to check `ok`
    # before saving anything -- parsed is not itself gated), but at least
    # the bad one must never silently appear as if it were valid.
    assert "discovery_timeout" not in parsed


def test_validate_settings_only_validates_keys_present():
    ok, errors, parsed = config.validate_settings({"client_id": "5"})
    assert ok
    assert list(parsed.keys()) == ["client_id"]
