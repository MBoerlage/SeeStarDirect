"""
ASCOM Alpaca FilterWheel interface (IFilterWheelV2).

The S30 Pro exposes one FilterWheel (verified live 2026-08-17):
  Names = ['Dark', 'IR', 'LP'], FocusOffsets = [0, 0, 0]
"""

from .device import AlpacaDevice, alpaca_property


class AlpacaFilterWheel(AlpacaDevice):
    device_type = "filterwheel"

    names = alpaca_property("names")
    focusoffsets = alpaca_property("focusoffsets")

    @property
    def position(self):
        return self.get("position")

    @position.setter
    def position(self, value):
        self.put("position", Position=int(value))
