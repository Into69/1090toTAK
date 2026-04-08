import logging

from .base import BaseReceiver
from .sbs_receiver import SBSReceiver
from .avr_receiver import AVRReceiver
from config import AppConfig, RECEIVER_SBS, RECEIVER_AVR, RECEIVER_AVR_SUBPROCESS, RECEIVER_RTLSDR, RECEIVER_JSON, RECEIVER_HACKRF, RECEIVER_USRP
from aircraft.registry import AircraftRegistry
from capabilities import HAS_RTLSDR, HAS_HACKRF, HAS_UHD

log = logging.getLogger(__name__)


def build_receiver(config: AppConfig, registry: AircraftRegistry) -> BaseReceiver:
    t = config.receiver.type

    if t in (RECEIVER_AVR, RECEIVER_AVR_SUBPROCESS):
        return AVRReceiver(registry, config)

    elif t == RECEIVER_RTLSDR:
        if not HAS_RTLSDR:
            log.warning(
                "pyrtlsdr is not installed — RTL-SDR receiver unavailable. "
                "Falling back to SBS. Install with: pip install pyrtlsdr"
            )
            return SBSReceiver(registry, config)
        from .rtlsdr_receiver import RTLSDRReceiver
        return RTLSDRReceiver(registry, config)

    elif t == RECEIVER_HACKRF:
        if not HAS_HACKRF:
            log.warning(
                "hackrf library not installed — HackRF receiver unavailable. "
                "Falling back to SBS. Install with: pip install hackrf"
            )
            return SBSReceiver(registry, config)
        from .hackrf_receiver import HackRFReceiver
        return HackRFReceiver(registry, config)

    elif t == RECEIVER_USRP:
        if not HAS_UHD:
            log.warning(
                "uhd library not installed — USRP receiver unavailable. "
                "Falling back to SBS. Install with: sudo apt install python3-uhd"
            )
            return SBSReceiver(registry, config)
        from .usrp_receiver import USRPReceiver
        return USRPReceiver(registry, config)

    elif t == RECEIVER_JSON:
        from .json_receiver import JSONReceiver
        return JSONReceiver(registry, config)

    else:
        return SBSReceiver(registry, config)
