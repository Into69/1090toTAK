import logging

from .base import BaseReceiver
from .sbs_receiver import SBSReceiver
from .avr_receiver import AVRReceiver
from config import AppConfig, RECEIVER_SBS, RECEIVER_AVR, RECEIVER_AVR_SUBPROCESS, RECEIVER_RTLSDR, RECEIVER_JSON, RECEIVER_BEAST
from aircraft.registry import AircraftRegistry
from capabilities import HAS_RTLSDR

log = logging.getLogger(__name__)


def build_receiver(config: AppConfig, registry: AircraftRegistry) -> BaseReceiver:
    t = config.receiver.type

    if t in (RECEIVER_AVR, RECEIVER_AVR_SUBPROCESS):
        return AVRReceiver(registry, config)

    elif t == RECEIVER_BEAST:
        from .beast_receiver import BeastReceiver
        return BeastReceiver(registry, config)

    elif t == RECEIVER_RTLSDR:
        if not HAS_RTLSDR:
            log.warning(
                "librtlsdr not found — RTL-SDR receiver unavailable. "
                "Falling back to SBS. Install the system library:\n"
                "  Windows: place rtlsdr.dll on PATH or in project root\n"
                "  Linux:   sudo apt install librtlsdr-dev\n"
                "  macOS:   brew install librtlsdr"
            )
            return SBSReceiver(registry, config)
        from .rtlsdr_receiver import RTLSDRReceiver
        return RTLSDRReceiver(registry, config)

    elif t == RECEIVER_JSON:
        from .json_receiver import JSONReceiver
        return JSONReceiver(registry, config)

    else:
        return SBSReceiver(registry, config)
