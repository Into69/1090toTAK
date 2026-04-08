"""
ReceiverManager — holds the active receiver in a mutable slot so it can be
stopped and replaced without restarting the process.

MultiReceiverManager — runs multiple receivers simultaneously, merging data
into a single AircraftRegistry.
"""

import logging
import threading
from dataclasses import asdict

from receivers import build_receiver
from config import ReceiverConfig

log = logging.getLogger(__name__)


class ReceiverManager:
    def __init__(self, config, registry):
        self._config = config
        self._registry = registry
        self._receiver = None
        self._active_type = None
        self._lock = threading.Lock()

    @property
    def receiver(self):
        return self._receiver

    @property
    def active_type(self):
        return self._active_type

    def start(self):
        with self._lock:
            self._receiver = build_receiver(self._config, self._registry)
            self._active_type = self._config.receiver.type
            self._receiver.start()
            log.info("Receiver started: %s", self._active_type)

    def restart(self):
        """Stop current receiver and start a fresh one built from current config."""
        with self._lock:
            old = self._receiver
            if old is not None:
                try:
                    old.stop()
                except Exception as e:
                    log.warning("Error stopping old receiver: %s", e)
                # Wait for the thread to fully exit so the USB device (RTL-SDR)
                # is released before the new receiver tries to open it.
                # A 5-second timeout is generous; cancel_read_async is near-instant.
                if old.is_alive():
                    old.join(timeout=5.0)
                    if old.is_alive():
                        log.warning("Old receiver thread did not exit within 5 s")
            self._receiver = build_receiver(self._config, self._registry)
            self._active_type = self._config.receiver.type
            self._receiver.start()
            log.info("Receiver restarted: %s", self._active_type)

    def stop(self):
        with self._lock:
            if self._receiver is not None:
                self._receiver.stop()
                self._receiver = None

    # ── Proxy the most-used BaseReceiver attributes ──────────────────────────
    # These allow existing code that holds a reference to the manager to call
    # .status(), .reconnect(), etc. without knowing about the swap.

    def status(self):
        r = self._receiver
        return r.status() if r is not None else {"connected": False, "source": "none"}

    def reconnect(self):
        """Called by routes when only connection params (host/port) changed."""
        r = self._receiver
        if r is not None:
            r.reconnect()

    def __getattr__(self, name):
        """Fall through unknown attribute access to the active receiver."""
        r = object.__getattribute__(self, "_receiver")
        if r is not None and hasattr(r, name):
            return getattr(r, name)
        raise AttributeError(f"ReceiverManager has no attribute {name!r}")


def _filter_receiver_fields(data: dict) -> dict:
    """Extract only valid ReceiverConfig fields from a dict."""
    valid = set(ReceiverConfig.__dataclass_fields__)
    return {k: v for k, v in data.items() if k in valid}


class MultiReceiverManager:
    """Runs multiple receivers simultaneously, all feeding the same registry."""

    def __init__(self, config, registry):
        self._config = config
        self._registry = registry
        self._receivers: dict = {}   # id -> BaseReceiver
        self._lock = threading.Lock()

    def _get_configs(self) -> list:
        """Return active receiver configs. Falls back to single receiver if list is empty."""
        if self._config.receivers:
            return [r for r in self._config.receivers if r.get("enabled", True)]
        return [{"id": "default", "label": "Default", **asdict(self._config.receiver), "enabled": True}]

    def start(self):
        with self._lock:
            for rx_cfg in self._get_configs():
                self._start_one(rx_cfg)

    def _start_one(self, rx_cfg: dict):
        rx_id = rx_cfg.get("id", "default")
        rc = ReceiverConfig(**_filter_receiver_fields(rx_cfg))
        old_rc = self._config.receiver
        self._config.receiver = rc
        try:
            rx = build_receiver(self._config, self._registry)
            rx.start()
            self._receivers[rx_id] = rx
            log.info("MultiReceiver: started %s (%s)", rx_cfg.get("label", rx_id), rc.type)
        finally:
            self._config.receiver = old_rc

    def stop(self):
        with self._lock:
            for rx_id, rx in self._receivers.items():
                try:
                    rx.stop()
                except Exception as e:
                    log.warning("MultiReceiver: error stopping %s: %s", rx_id, e)
            self._receivers.clear()

    def restart(self):
        self.stop()
        self.start()

    def status(self):
        statuses = {}
        total_msgs = 0
        any_connected = False
        for rx_id, rx in self._receivers.items():
            s = rx.status()
            statuses[rx_id] = s
            total_msgs += s.get("messages", 0)
            if s.get("connected"):
                any_connected = True
        # Return a combined status compatible with single-receiver API
        first = list(self._receivers.values())[0] if self._receivers else None
        return {
            "connected": any_connected,
            "messages": total_msgs,
            "source": f"{len(self._receivers)} receivers" if len(self._receivers) > 1
                      else (first.status().get("source", "?") if first else "none"),
            "receivers": statuses,
        }

    def reconnect(self):
        for rx in self._receivers.values():
            try:
                rx.reconnect()
            except Exception:
                pass

    @property
    def receiver(self):
        """Return the first receiver for spectrum/signal_quality access."""
        if self._receivers:
            return list(self._receivers.values())[0]
        return None

    def __getattr__(self, name):
        """Proxy unknown attributes to the first receiver."""
        receivers = object.__getattribute__(self, "_receivers")
        if receivers:
            first = list(receivers.values())[0]
            if hasattr(first, name):
                return getattr(first, name)
        raise AttributeError(f"MultiReceiverManager has no attribute {name!r}")
