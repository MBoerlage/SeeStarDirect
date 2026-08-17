"""
ASCOM Alpaca Switch interface (ISwitchV2).

The S30 Pro exposes one Switch device with one switch (verified live
2026-08-17): MaxSwitch=1, switch 0 = "Dew heater" (boolean, writable,
Min=0 Max=1 Step=1). SupportedActions=['OCHTestPowerReport'] -- a
ZWO-proprietary action, not wrapped here; use the raw Action diagnostic menu
if you need it.
"""

from .device import AlpacaDevice


class AlpacaSwitch(AlpacaDevice):
    device_type = "switch"

    @property
    def maxswitch(self):
        return self.get("maxswitch")

    def get_switch_name(self, idx):
        return self.get("getswitchname", Id=idx)

    def get_switch_description(self, idx):
        return self.get("getswitchdescription", Id=idx)

    def get_switch(self, idx):
        return self.get("getswitch", Id=idx)

    def get_switch_value(self, idx):
        return self.get("getswitchvalue", Id=idx)

    def min_switch_value(self, idx):
        return self.get("minswitchvalue", Id=idx)

    def max_switch_value(self, idx):
        return self.get("maxswitchvalue", Id=idx)

    def switch_step(self, idx):
        return self.get("switchstep", Id=idx)

    def can_write(self, idx):
        return self.get("canwrite", Id=idx)

    def set_switch(self, idx, state):
        return self.put("setswitch", Id=idx, State=str(bool(state)).lower())

    def set_switch_value(self, idx, value):
        return self.put("setswitchvalue", Id=idx, Value=value)
