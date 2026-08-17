"""
Core ASCOM Alpaca transaction layer.

Every device class (Telescope, Camera, Focuser, FilterWheel, Switch) is built
on top of AlpacaDevice, which implements the actual HTTP mechanics required by
the ASCOM Alpaca specification:

  - ClientID / ClientTransactionID / ServerTransactionID bookkeeping
  - ErrorNumber / ErrorMessage inspection on every call (HTTP 200 does NOT
    mean the operation succeeded -- Alpaca reports device-level failures in
    the JSON body)
  - DEBUG-level request/response logging (with ImageArray payloads summarized
    instead of dumped in full)
"""

import itertools
import logging
import time

import requests

logger = logging.getLogger("alpaca.http")

_txn_counter = itertools.count(1)


def next_transaction_id():
    return next(_txn_counter)


# Common ASCOM Alpaca ErrorNumbers (see the Alpaca API spec / ASCOM
# ASCOM.Common.Alpaca.AlpacaErrors). Not exhaustive -- just enough to make
# error output self-explanatory without looking anything up.
ERROR_NAMES = {
    0x400: "NotImplemented",
    0x401: "InvalidValue",
    0x402: "ValueNotSet",
    0x407: "NotConnected",
    0x408: "InvalidWhileParked",
    0x409: "InvalidWhileSlaved",
    0x40B: "InvalidOperation",
    0x40C: "ActionNotImplemented",
}


class AlpacaError(Exception):
    """Raised when an Alpaca call returns a non-zero ErrorNumber, or the
    HTTP transport itself fails."""

    def __init__(self, error_number, error_message, member="", url=""):
        self.error_number = error_number
        self.error_message = error_message
        self.member = member
        self.url = url
        name = ERROR_NAMES.get(error_number, "")
        suffix = f" ({name})" if name else ""
        super().__init__(f"{member}: ErrorNumber={error_number}{suffix} {error_message}")


def alpaca_property(member):
    """Class-body helper: declares a read-only property backed by a GET call.

    Usage: ``atpark = alpaca_property("atpark")`` inside a device subclass.
    """

    def getter(self):
        return self.get(member)

    getter.__name__ = member
    return property(getter)


def _summarize_value(value):
    """Never let a multi-megapixel ImageArray hit the log file -- just
    record its shape/type."""
    dims = []
    v = value
    while isinstance(v, list):
        dims.append(len(v))
        v = v[0] if v else None
    if dims:
        return f"<array dims={dims} sample_type={type(v).__name__}>"
    return value


class AlpacaDevice:
    """Base class for one Alpaca device instance (e.g. Telescope 0)."""

    device_type = "device"  # overridden by subclasses

    def __init__(self, base_url, device_number, client_id, session=None, timeout=10):
        self.base_url = base_url.rstrip("/")
        self.device_number = device_number
        self.client_id = client_id
        self.session = session or requests.Session()
        self.timeout = timeout
        self.raw_info = {}  # populated from /management/v1/configureddevices

    def label(self):
        return f"{self.device_type.capitalize()} {self.device_number}"

    def display_name(self):
        return self.raw_info.get("DeviceName", self.label())

    def _url(self, member):
        return f"{self.base_url}/api/v1/{self.device_type}/{self.device_number}/{member}"

    # ------------------------------------------------------------------
    # Normal calls: raise AlpacaError on any failure (transport or device)
    # ------------------------------------------------------------------

    def get(self, member, timeout=None, **params):
        return self._request("GET", member, params=params, timeout=timeout)

    def put(self, member, timeout=None, **data):
        return self._request("PUT", member, data=data, timeout=timeout)

    def _request(self, method, member, params=None, data=None, timeout=None):
        url = self._url(member)
        payload = dict(params or data or {})
        payload.setdefault("ClientID", self.client_id)
        payload.setdefault("ClientTransactionID", next_transaction_id())

        t0 = time.time()
        try:
            if method == "GET":
                resp = self.session.get(url, params=payload, timeout=timeout or self.timeout)
            else:
                resp = self.session.put(url, data=payload, timeout=timeout or self.timeout)
        except requests.RequestException as exc:
            elapsed = (time.time() - t0) * 1000
            logger.debug("%s %s data=%s -> TRANSPORT EXCEPTION %s (%.1fms)",
                         method, url, payload, exc, elapsed)
            raise

        elapsed = (time.time() - t0) * 1000
        try:
            body = resp.json()
        except ValueError:
            body = {"_raw_text": resp.text}

        log_body = body
        if member.lower() == "imagearray" and isinstance(body, dict) and "Value" in body:
            log_body = dict(body)
            log_body["Value"] = _summarize_value(body["Value"])
        logger.debug("%s %s data=%s -> HTTP %s %s (%.1fms)",
                     method, url, payload, resp.status_code, log_body, elapsed)

        if resp.status_code >= 400:
            raise AlpacaError(body.get("ErrorNumber", -1),
                               body.get("ErrorMessage", resp.text), member, url)

        err_num = body.get("ErrorNumber", 0)
        if err_num:
            raise AlpacaError(err_num, body.get("ErrorMessage", ""), member, url)

        return body.get("Value")

    # ------------------------------------------------------------------
    # Raw calls for the diagnostic menu: never raise, just return everything
    # ------------------------------------------------------------------

    def raw_get(self, member, **params):
        return self._raw_request("GET", member, params)

    def raw_put(self, member, **data):
        return self._raw_request("PUT", member, data)

    def _raw_request(self, method, member, payload):
        url = self._url(member)
        payload = dict(payload)
        payload.setdefault("ClientID", self.client_id)
        payload.setdefault("ClientTransactionID", next_transaction_id())

        t0 = time.time()
        if method == "GET":
            resp = self.session.get(url, params=payload, timeout=self.timeout)
        else:
            resp = self.session.put(url, data=payload, timeout=self.timeout)
        elapsed = (time.time() - t0) * 1000

        try:
            body = resp.json()
        except ValueError:
            body = {"_raw_text": resp.text}

        logger.debug("RAW %s %s data=%s -> HTTP %s %s (%.1fms)",
                     method, url, payload, resp.status_code, body, elapsed)
        return resp.status_code, body, elapsed

    # ------------------------------------------------------------------
    # Common ASCOM properties (every device type has these)
    # ------------------------------------------------------------------

    @property
    def connected(self):
        return bool(self.get("connected"))

    @connected.setter
    def connected(self, value):
        self.put("connected", Connected=str(bool(value)).lower())

    name = alpaca_property("name")
    description = alpaca_property("description")
    driverinfo = alpaca_property("driverinfo")
    driverversion = alpaca_property("driverversion")
    interfaceversion = alpaca_property("interfaceversion")
    supportedactions = alpaca_property("supportedactions")
