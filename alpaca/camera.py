"""
ASCOM Alpaca Camera interface (ICameraV3 subset).

The S30 Pro exposes TWO camera devices (verified live 2026-08-17):

  Camera 0 - "...Telephoto Camera": 2160x3840, 2.9um pixels, ExposureMax=2000s
  Camera 1 - "...Wide Angle Camera": 2160x3840, 1.6um pixels, ExposureMax=60s

Both report SensorType=2 (RGGB Bayer) -- ImageArray returns the RAW,
undebayered mosaic (Rank=2), not a 3-plane RGB array. That's normal for a
one-shot-color astro camera and is exactly what you want preserved in FITS.

Not implemented on this driver (returns ErrorNumber 1024/NotImplemented):
  Gains (the list-of-named-gains property), Offset.
Gain itself IS implemented as a plain numeric property (0-600).
Binning is fixed at 1x1 (MaxBinX = MaxBinY = 1).
No cooler (CanGetCoolerPower=False, CanSetCCDTemperature=False).
"""

from .device import AlpacaDevice, alpaca_property


class AlpacaCamera(AlpacaDevice):
    device_type = "camera"

    cameraxsize = alpaca_property("cameraxsize")
    cameraysize = alpaca_property("cameraysize")
    pixelsizex = alpaca_property("pixelsizex")
    pixelsizey = alpaca_property("pixelsizey")
    sensortype = alpaca_property("sensortype")
    sensorname = alpaca_property("sensorname")
    maxadu = alpaca_property("maxadu")
    exposuremin = alpaca_property("exposuremin")
    exposuremax = alpaca_property("exposuremax")
    exposureresolution = alpaca_property("exposureresolution")
    canabortexposure = alpaca_property("canabortexposure")
    canstopexposure = alpaca_property("canstopexposure")
    cangetcoolerpower = alpaca_property("cangetcoolerpower")
    cansetccdtemperature = alpaca_property("cansetccdtemperature")
    gainmin = alpaca_property("gainmin")
    gainmax = alpaca_property("gainmax")
    binx = alpaca_property("binx")
    biny = alpaca_property("biny")
    maxbinx = alpaca_property("maxbinx")
    maxbiny = alpaca_property("maxbiny")
    imageready = alpaca_property("imageready")
    camerastate = alpaca_property("camerastate")

    @property
    def gain(self):
        return self.get("gain")

    @gain.setter
    def gain(self, value):
        self.put("gain", Gain=int(value))

    def start_exposure(self, duration_s, light=True):
        return self.put("startexposure", Duration=duration_s, Light=str(bool(light)).lower())

    def abort_exposure(self):
        return self.put("abortexposure")

    def stop_exposure(self):
        return self.put("stopexposure")

    def get_image_array(self, timeout=60):
        """Pulls the full pixel array over Alpaca. Can be large -- give it a
        generous timeout."""
        return self.get("imagearray", timeout=timeout)
