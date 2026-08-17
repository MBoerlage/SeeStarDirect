"""
ASCOM Alpaca Focuser interface (IFocuserV3).

The S30 Pro exposes TWO focusers (verified live 2026-08-17), one per camera:

  Focuser 0 - Telephoto: MaxStep=2600, Absolute=True
  Focuser 1 - Wide Angle: MaxStep=1023, Absolute=True

Neither reports StepSize (ErrorNumber 1024/NotImplemented) or temperature
compensation (TempCompAvailable=False). Temperature IS readable on Focuser 0.
"""

from .device import AlpacaDevice, alpaca_property


class AlpacaFocuser(AlpacaDevice):
    device_type = "focuser"

    position = alpaca_property("position")
    maxstep = alpaca_property("maxstep")
    maxincrement = alpaca_property("maxincrement")
    ismoving = alpaca_property("ismoving")
    absolute = alpaca_property("absolute")
    tempcomp = alpaca_property("tempcomp")
    tempcompavailable = alpaca_property("tempcompavailable")
    temperature = alpaca_property("temperature")

    def move(self, position):
        """Absolute move (this focuser reports Absolute=True)."""
        return self.put("move", Position=int(position))

    def halt(self):
        return self.put("halt")
