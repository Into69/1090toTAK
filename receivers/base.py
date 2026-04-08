import threading
import time
import logging
from abc import ABC, abstractmethod

from aircraft.registry import AircraftRegistry
from config import AppConfig

log = logging.getLogger(__name__)


class BaseReceiver(threading.Thread, ABC):
    def __init__(self, registry: AircraftRegistry, config: AppConfig):
        super().__init__(daemon=True, name=self.__class__.__name__)
        self.registry = registry
        self.config = config
        self._stop_event = threading.Event()
        self._reconnect_event = threading.Event()
        self.connected = False
        self.message_count = 0
        self.error_count = 0

    def stop(self) -> None:
        self._stop_event.set()

    def stopped(self) -> bool:
        return self._stop_event.is_set()

    def reconnect(self) -> None:
        """Signal the receiver to drop its current connection and reconnect."""
        self._reconnect_event.set()

    @abstractmethod
    def run(self) -> None:
        pass

    def _reconnect_loop(self, connect_fn, label: str) -> None:
        delay = 1.0
        max_delay = 60.0
        attempt = 0
        while not self.stopped():
            self._reconnect_event.clear()
            try:
                # Log first attempt at INFO; subsequent retries at DEBUG
                if attempt == 0:
                    log.info("%s: connecting...", label)
                else:
                    log.debug("%s: reconnecting (attempt %d)...", label, attempt + 1)
                attempt += 1
                connect_fn()
                attempt = 0  # reset on clean disconnect so next connect logs at INFO
                delay = 1.0
            except Exception as e:
                self.connected = False
                self.error_count += 1
                if not self.stopped():
                    log.debug("%s: error (%s), retrying in %.0fs", label, e, delay)
                    self._stop_event.wait(delay)
                    delay = min(delay * 2, max_delay)
            # Config change requested an immediate reconnect — skip backoff
            if self._reconnect_event.is_set():
                self.connected = False
                delay = 1.0

    def status(self) -> dict:
        return {
            "type": self.__class__.__name__,
            "source": self.config.receiver.type,
            "connected": self.connected,
            "messages": self.message_count,
            "errors": self.error_count,
        }
