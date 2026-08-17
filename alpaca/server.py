"""
AlpacaServer: wraps one Alpaca server's /management endpoints and acts as a
factory for typed device objects (AlpacaTelescope, AlpacaCamera, ...).
"""

import logging

import requests

from .camera import AlpacaCamera
from .device import AlpacaDevice, next_transaction_id
from .filterwheel import AlpacaFilterWheel
from .focuser import AlpacaFocuser
from .switch import AlpacaSwitch
from .telescope import AlpacaTelescope

logger = logging.getLogger("alpaca.http")

DEVICE_CLASSES = {
    "telescope": AlpacaTelescope,
    "camera": AlpacaCamera,
    "focuser": AlpacaFocuser,
    "filterwheel": AlpacaFilterWheel,
    "switch": AlpacaSwitch,
}


class AlpacaServer:
    def __init__(self, ip, port, client_id, session=None, timeout=10):
        self.ip = ip
        self.port = port
        self.base_url = f"http://{ip}:{port}"
        self.client_id = client_id
        self.session = session or requests.Session()
        self.timeout = timeout

        self.api_versions = []
        self.description = {}
        self.configured_devices = []

    def _management_get(self, path):
        url = f"{self.base_url}{path}"
        params = {"ClientID": self.client_id, "ClientTransactionID": next_transaction_id()}
        resp = self.session.get(url, params=params, timeout=self.timeout)
        resp.raise_for_status()
        body = resp.json()
        logger.debug("GET %s data=%s -> HTTP %s %s", url, params, resp.status_code, body)
        return body

    def query_management(self):
        """Populates api_versions, description, configured_devices. Raises
        requests.RequestException if the server can't be reached."""
        self.api_versions = self._management_get("/management/apiversions").get("Value", [])
        self.description = self._management_get("/management/v1/description").get("Value", {})
        self.configured_devices = self._management_get(
            "/management/v1/configureddevices"
        ).get("Value", [])
        return self.configured_devices

    def make_device(self, device_info):
        dtype = device_info["DeviceType"].lower()
        dnum = device_info["DeviceNumber"]
        cls = DEVICE_CLASSES.get(dtype, AlpacaDevice)
        dev = cls(self.base_url, dnum, self.client_id, session=self.session, timeout=self.timeout)
        dev.device_type = dtype  # for the fallback AlpacaDevice case
        dev.raw_info = device_info
        return dev

    def devices(self):
        return [self.make_device(d) for d in self.configured_devices]

    def devices_by_type(self, device_type):
        return [
            self.make_device(d)
            for d in self.configured_devices
            if d["DeviceType"].lower() == device_type.lower()
        ]
