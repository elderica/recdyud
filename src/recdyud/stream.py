"""Background reading of the TS endpoint."""

import array
import logging
import queue
import threading
import time
from collections.abc import Iterator

from .device import TS_READ_SIZE, DyUd200
from .ts import PACKET_SIZE

log = logging.getLogger(__name__)

QUEUE_CHUNKS = 1536  # 24 MiB, several seconds of stream


class TsReader(threading.Thread):
    """Reads the TS endpoint continuously so that the tuner's FIFO never overflows.

    Chunks are handed to the consumer through :attr:`queue`.  When the reader
    fails, :attr:`error` is set and ``on_error`` (usually the global stop
    event) is set.
    """

    def __init__(self, tuner: DyUd200, on_error: threading.Event) -> None:
        super().__init__(name="ts-reader", daemon=True)
        self.tuner = tuner
        self.on_error = on_error
        self.queue: queue.Queue[bytes] = queue.Queue(QUEUE_CHUNKS)
        self.overflows = 0
        self.timeouts = 0
        self.bytes = 0
        self.max_gap = 0.0  # longest time between two reads, seconds
        self.error: BaseException | None = None
        self._halt = threading.Event()

    def run(self) -> None:
        buf = array.array("B", bytes(TS_READ_SIZE))
        last = None
        try:
            while not self._halt.is_set():
                now = time.monotonic()
                if last is not None:
                    self.max_gap = max(self.max_gap, now - last)
                n = self.tuner.read_ts(buf, timeout=500)
                last = time.monotonic()
                if n == 0:
                    self.timeouts += 1
                    if self.timeouts % 10 == 0:
                        log.warning("no TS data from the tuner for %d reads", self.timeouts)
                    continue
                self.timeouts = 0
                self.bytes += n
                try:
                    self.queue.put_nowait(buf[:n].tobytes())
                except queue.Full:
                    self.overflows += 1
                    if self.overflows == 1 or self.overflows % 100 == 0:
                        log.warning("consumer is too slow; dropped %d chunks", self.overflows)
        except BaseException as e:
            self.error = e
            self.on_error.set()

    def halt(self, timeout: float = 2.0) -> None:
        self._halt.set()
        if self.is_alive():
            self.join(timeout)

    def drain(self) -> Iterator[bytes]:
        """Yield the chunks that are still queued (call after :meth:`halt`)."""
        while True:
            try:
                yield self.queue.get_nowait()
            except queue.Empty:
                return


def purge(tuner: DyUd200, packets: int) -> None:
    buf = array.array("B", bytes(TS_READ_SIZE))
    remaining = packets * PACKET_SIZE
    deadline = time.monotonic() + 3.0
    while remaining > 0 and time.monotonic() < deadline:
        remaining -= tuner.read_ts(buf, timeout=500)
