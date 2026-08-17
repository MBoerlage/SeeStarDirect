"""Persistent configuration (config.json next to this file).

The Alpaca port is intentionally NOT stored persistently -- discovery is
supposed to determine it each run, per the Alpaca spec. preferred_server_ip
/ preferred_server_port are only a manual fallback for when discovery fails
(e.g. broadcast blocked by network config).
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
}


def load_config(path=CONFIG_PATH):
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
