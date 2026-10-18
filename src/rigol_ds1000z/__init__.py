"""Fast LAN control of RIGOL DS1000Z oscilloscopes over raw SCPI sockets."""

from .discovery import discover
from .scope import DS1000Z, ChannelOff, MEASURE_ITEMS
from .transport import ScpiError, ScpiTimeout, ScpiTransport
from .waveform import Preamble, Waveform, analyze

__all__ = [
    "DS1000Z", "ChannelOff", "MEASURE_ITEMS", "ScpiError", "ScpiTimeout", "ScpiTransport",
    "Preamble", "Waveform", "analyze", "discover",
]
__version__ = "0.1.0"
