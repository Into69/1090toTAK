"""
ReceiverManager — holds the active receiver in a mutable slot so it can be
stopped and replaced without restarting the process.

Usage:
    mgr = ReceiverManager(config, registry)
    mgr.start()          # build and start initial receiver
    mgr.restart()        # stop current, build new from current config, start
    mgr.stop()           # stop current receiver permanently

Routes / config changes call mgr.restart() instead of receiver.reconnect().
The manager exposes .receiver as a property so the rest of the app always
gets the current instance.
"""

import logging
import threading

from receivers import build_receiver

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
