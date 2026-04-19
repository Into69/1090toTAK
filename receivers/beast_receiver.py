"""
Beast binary receiver — dump1090 / readsb TCP port 30005.

Beast frame format (Mode-S Beast / "BEAST" output):

    0x1A | type | timestamp(6) | signal(1) | data(N)

  type byte (ASCII): '1' (Mode AC, 2 bytes data) | '2' (Mode S short, 7 bytes)
                     '3' (Mode S long,  14 bytes) | '4' (status/config, 14 bytes)

  Inside the timestamp/signal/data section, any literal 0x1A byte is escaped
  by doubling: 0x1A 0x1A => single 0x1A. Frames are framed by the unescaped
  leading 0x1A.

We reuse AVRReceiver._parse_avr() for the actual Mode S decoding by handing it
the hex string of the data payload — the existing decoder handles CRC, DF
dispatch, position decoding, etc. identically to the AVR path.
"""

import logging
import socket

from .avr_receiver import AVRReceiver

log = logging.getLogger(__name__)

_TYPE_LEN = {0x31: 2, 0x32: 7, 0x33: 14, 0x34: 14}   # '1','2','3','4'


class BeastReceiver(AVRReceiver):
    """Beast binary protocol over TCP."""

    def run(self) -> None:
        self._reconnect_loop(self._connect_tcp_beast, "Beast")

    def _connect_tcp_beast(self) -> None:
        host = self.config.receiver.host
        port = self.config.receiver.beast_port
        with socket.create_connection((host, port), timeout=10) as sock:
            sock.settimeout(30)
            self.connected = True
            log.info("Beast: connected to %s:%d", host, port)
            buf = bytearray()
            while not self.stopped() and not self._reconnect_event.is_set():
                try:
                    chunk = sock.recv(8192)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                buf.extend(chunk)
                self._consume_beast(buf)
        self.connected = False

    def _consume_beast(self, buf: bytearray) -> None:
        """Pull complete Beast frames out of buf, leaving any partial trailing bytes."""
        i = 0
        n = len(buf)
        while True:
            # Find next unescaped 0x1A frame start
            while i < n and buf[i] != 0x1A:
                i += 1
            if i >= n - 1:
                break
            type_byte = buf[i + 1]
            data_len = _TYPE_LEN.get(type_byte)
            if data_len is None:
                # Unknown / corrupted — skip this 0x1A and keep scanning
                i += 1
                continue
            payload_len = 6 + 1 + data_len   # timestamp + signal + data
            # Walk forward unescaping; a doubled 0x1A counts as one byte
            j = i + 2
            unescaped = bytearray()
            while len(unescaped) < payload_len and j < n:
                b = buf[j]
                if b == 0x1A:
                    if j + 1 >= n:
                        # Trailing 0x1A — need more bytes to know if it's escape or new frame
                        return self._compact(buf, i)
                    if buf[j + 1] == 0x1A:
                        unescaped.append(0x1A)
                        j += 2
                        continue
                    # Bare 0x1A inside payload = next frame starts here; current frame is corrupt
                    break
                unescaped.append(b)
                j += 1
            if len(unescaped) < payload_len:
                if j >= n:
                    # Need more bytes
                    return self._compact(buf, i)
                # Truncated by stray 0x1A — discard this frame, advance one
                i += 1
                continue
            data = unescaped[7:7 + data_len]   # skip 6-byte timestamp + 1-byte signal
            if data_len in (7, 14):
                hex_str = data.hex().upper()
                self._parse_avr(f"*{hex_str};")
            i = j

        self._compact(buf, i)

    @staticmethod
    def _compact(buf: bytearray, keep_from: int) -> None:
        """Drop bytes [0:keep_from) from buf in place."""
        if keep_from > 0:
            del buf[:keep_from]

    def status(self) -> dict:
        s = super().status()
        s["url"] = f"tcp://{self.config.receiver.host}:{self.config.receiver.beast_port}"
        return s
