"""
ASCOM Alpaca Telescope interface (ITelescopeV3).

Property/method set below matches what the S30 Pro's native Alpaca server
actually reports (verified live 2026-08-17):

  InterfaceVersion=3, DriverInfo="Telescope V3"
  CanPark=True  CanUnpark=True  CanFindHome=True
  CanSlew=True  CanSlewAsync=True  CanSync=True  CanSetTracking=True
  CanPulseGuide=True
  CanSetPierSide=False  CanSyncAltAz=False
  CanSlewAltAz=False    CanSlewAltAzAsync=False
  SupportedActions=[]   (no ZWO-proprietary actions on this device)

SideOfPier is explicitly NOT implemented on this driver (alt-az mount) --
ErrorNumber 1024 (NotImplemented) if you query it, which is expected and not
a bug.

AtHome and MoveAxis/CanMoveAxis/AxisRates are wired up below but NOT yet
confirmed against this hardware -- treat their behavior as unverified until
tested (see diagnostics.run_first_slew_test / run_moveaxis_test).
"""

from .device import AlpacaDevice, alpaca_property


class AlpacaTelescope(AlpacaDevice):
    device_type = "telescope"

    # --- capability flags ---
    canpark = alpaca_property("canpark")
    canunpark = alpaca_property("canunpark")
    canfindhome = alpaca_property("canfindhome")
    canslew = alpaca_property("canslew")
    canslewasync = alpaca_property("canslewasync")
    cansync = alpaca_property("cansync")
    cansettracking = alpaca_property("cansettracking")
    canpulseguide = alpaca_property("canpulseguide")
    cansetpierside = alpaca_property("cansetpierside")
    cansyncaltaz = alpaca_property("cansyncaltaz")
    canslewaltaz = alpaca_property("canslewaltaz")
    canslewaltazasync = alpaca_property("canslewaltazasync")

    # --- state ---
    atpark = alpaca_property("atpark")
    athome = alpaca_property("athome")
    slewing = alpaca_property("slewing")
    rightascension = alpaca_property("rightascension")
    declination = alpaca_property("declination")
    altitude = alpaca_property("altitude")
    azimuth = alpaca_property("azimuth")
    equatorialsystem = alpaca_property("equatorialsystem")
    trackingrates = alpaca_property("trackingrates")
    utcdate = alpaca_property("utcdate")
    siderealtime = alpaca_property("siderealtime")

    @property
    def tracking(self):
        return self.get("tracking")

    @tracking.setter
    def tracking(self, value):
        self.put("tracking", Tracking=str(bool(value)).lower())

    # --- methods ---
    def unpark(self):
        return self.put("unpark")

    def park(self):
        return self.put("park")

    def find_home(self):
        return self.put("findhome")

    def abort_slew(self):
        return self.put("abortslew")

    def slew_to_coordinates_async(self, ra_hours, dec_deg):
        return self.put("slewtocoordinatesasync", RightAscension=ra_hours, Declination=dec_deg)

    def sync_to_coordinates(self, ra_hours, dec_deg):
        return self.put("synctocoordinates", RightAscension=ra_hours, Declination=dec_deg)

    # --- axis-level control (ITelescopeV3 MoveAxis) ---
    def can_move_axis(self, axis):
        return self.get("canmoveaxis", Axis=axis)

    def axis_rates(self, axis):
        return self.get("axisrates", Axis=axis)

    def move_axis(self, axis, rate):
        return self.put("moveaxis", Axis=axis, Rate=rate)
