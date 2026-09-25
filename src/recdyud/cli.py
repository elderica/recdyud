"""recdyud - a recpt1-style tuner command for the DY-UD200.

    recdyud [options] CHANNEL RECTIME DESTFILE

RECTIME is a number of seconds or ``-`` (until killed); DESTFILE ``-`` means
stdout.  The received TS is descrambled with the B-CAS card inserted in the
tuner unless ``--no-b25`` is given.
"""

import argparse
import logging
import os
import queue
import signal
import sys
import threading
import time
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, version

import numpy as np

from .b25 import B25Error, Descrambler
from .bcas import BCasCard, CardError
from .channels import Channel, InvalidChannel, parse_channel
from .device import LOCK_THRESHOLD, DeviceError, DyUd200, open_device
from .stream import TsReader, purge
from .ts import NULL_PID, PACKET_SIZE, PacketAligner, TsAnalyzer

log = logging.getLogger("recdyud")

# Packets discarded right after the tuner locks (BonDriver_dyud does the same).
PURGE_PACKETS = 2048


def package_version() -> str:
    try:
        return version("recdyud")
    except PackageNotFoundError:
        return "unknown"


def setup_logging(verbose: int, quiet: bool) -> None:
    level = logging.WARNING if quiet else logging.DEBUG if verbose else logging.INFO
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("recdyud: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    if verbose < 2:
        logging.getLogger("usb").setLevel(logging.WARNING)


def parse_rectime(value: str) -> float | None:
    if value == "-":
        return None
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid rectime: {value!r}") from None
    if seconds <= 0:
        raise argparse.ArgumentTypeError("rectime must be positive or '-'")
    return seconds


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="recdyud",
        description="Receive ISDB-T with a DY-UD200 and write the TS (recpt1 compatible).",
        epilog="CHANNEL: 13-62 (UHF), C13-C63 (CATV), 1-12 (VHF) or a frequency such as 473143kHz. "
        "RECTIME: seconds or '-' (until killed). DESTFILE: a file or '-' (stdout).",
    )
    b25 = p.add_mutually_exclusive_group()
    b25.add_argument("--b25", action="store_true", help="descramble with the B-CAS card (default)")
    b25.add_argument("--no-b25", action="store_true", help="output the TS without descrambling")
    p.add_argument("--round", type=int, default=4, metavar="N", help="MULTI2 round (default: 4)")
    p.add_argument("--strip", action="store_true", help="strip null packets in the descrambler")
    p.add_argument("--EMM", "--emm", dest="emm", action="store_true", help="process EMM")
    p.add_argument(
        "--b25-timeout",
        type=float,
        default=5.0,
        metavar="SEC",
        help="pass the stream through when descrambling has not started after SEC seconds "
        "(PAT/PMT/ECM missing, e.g. poor reception); 0 waits as long as libaribb25 does (default: 5)",
    )
    p.add_argument(
        "--device",
        default=os.environ.get("RECDYUD_DEVICE"),
        metavar="DEV",
        help="auto (first free tuner, default), an index, BUS:ADDR, /dev/bus/usb/BBB/AAA or a "
        "port path like 5-1.2 (env: RECDYUD_DEVICE)",
    )
    p.add_argument(
        "--lock-timeout",
        type=float,
        default=10.0,
        metavar="SEC",
        help="give up when the tuner does not lock within SEC seconds (default: 10)",
    )
    p.add_argument(
        "--keep-null",
        action="store_true",
        help="keep null packets (PID 0x1FFF).  The DY-UD200 outputs the whole multiplex frame, so "
        "they are dropped by default (like BonDriver_dyud); the stream is about twice as large with them",
    )
    p.add_argument("-v", "--verbose", action="count", default=0, help="verbose logging (-vv for USB)")
    p.add_argument("-q", "--quiet", action="store_true", help="only print warnings and errors")
    p.add_argument("--version", action="version", version=f"%(prog)s {package_version()}")
    p.add_argument("channel")
    p.add_argument("rectime", type=parse_rectime)
    p.add_argument("destfile")
    return p


class Output:
    def __init__(self, dest: str) -> None:
        if dest == "-":
            self.fd = sys.stdout.fileno()
            self._close = False
        else:
            self.fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            self._close = True
        self.bytes = 0

    def write(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            n = os.write(self.fd, view)
            view = view[n:]
        self.bytes += len(data)

    def close(self) -> None:
        if self._close:
            os.close(self.fd)


class Pipeline:
    """Descrambling stage with recpt1-like fallback to scrambled output.

    libaribb25 holds back all data until it has seen PAT, PMT and ECM (up to
    16-32 MiB).  With poor reception these tables never arrive, so if nothing
    comes out within ``start_timeout`` seconds the stream is passed through.
    """

    def __init__(
        self,
        descrambler: Descrambler | None,
        start_timeout: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.descrambler = descrambler
        self.start_timeout = start_timeout
        self._clock = clock
        self._first_input: float | None = None
        self._producing = False

    def _fall_back(self, pending: bytes) -> bytes:
        if self.descrambler is not None:
            self.descrambler.close()
            self.descrambler = None
        return pending

    def process(self, packets: bytes) -> bytes:
        d = self.descrambler
        if d is None:
            return packets
        try:
            d.put(packets)
            out = d.get()
        except B25Error as e:
            log.error("descrambler error: %s; falling back to scrambled output", e)
            # Depending on where put() failed, the current chunk either is still
            # in the input buffer or has been rolled back.
            try:
                pending = d.withdraw()
            except B25Error:
                pending = b""
            if not pending.endswith(packets):
                pending += packets
            return self._fall_back(pending)

        if out:
            self._producing = True
        elif not self._producing and self.start_timeout > 0:
            now = self._clock()
            if self._first_input is None:
                self._first_input = now
            elif now - self._first_input >= self.start_timeout:
                log.error(
                    "descrambling did not start within %gs (PAT/PMT/ECM not received; check the reception "
                    "with recdyud-diag monitor); falling back to scrambled output",
                    self.start_timeout,
                )
                try:
                    pending = d.withdraw()  # includes the current chunk
                except B25Error:
                    pending = packets
                return self._fall_back(pending)
        return out

    def finish(self) -> bytes:
        d = self.descrambler
        if d is None:
            return b""
        try:
            return d.flush()
        except B25Error as e:
            log.error("descrambler flush failed: %s", e)
            try:
                return d.withdraw()
            except B25Error:
                return b""


def drop_null_packets(packets: bytes) -> bytes:
    n = len(packets) // PACKET_SIZE
    if n == 0:
        return packets
    a = np.frombuffer(packets, dtype=np.uint8).reshape(n, PACKET_SIZE)
    pid = ((a[:, 1] & 0x1F).astype(np.int32) << 8) | a[:, 2]
    keep = pid != NULL_PID
    return packets if keep.all() else a[keep].tobytes()


def setup_descrambler(tuner: DyUd200, args: argparse.Namespace) -> Descrambler | None:
    card = BCasCard(tuner)
    try:
        status = card.initialize()
    except (CardError, DeviceError) as e:
        log.warning("cannot initialise the B-CAS card: %s; falling back to scrambled output", e)
        return None
    if card.ids:
        log.debug("B-CAS card: %s", card.ids[0].masked)
    try:
        return Descrambler(card, status, card.ids, multi2_round=args.round, strip=args.strip, emm=args.emm)
    except B25Error as e:
        log.warning("cannot start the descrambler: %s; falling back to scrambled output", e)
        return None


def install_signal_handlers() -> threading.Event:
    stop = threading.Event()

    def on_signal(signum, _frame):
        log.debug("received %s", signal.Signals(signum).name)
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGUSR1):
        signal.signal(sig, on_signal)
    return stop


def record(args: argparse.Namespace) -> int:
    channel = parse_channel(args.channel)
    stop = install_signal_handlers()
    output = Output(args.destfile)
    try:
        with open_device(args.device) as tuner:
            log.info("using DY-UD200 %s, firmware %s", tuner.location, tuner.firmware)
            descrambler = None if args.no_b25 else setup_descrambler(tuner, args)
            pipeline = Pipeline(descrambler, start_timeout=args.b25_timeout)
            try:
                return stream(tuner, channel, pipeline, output, args, stop)
            finally:
                if pipeline.descrambler is not None:
                    pipeline.descrambler.close()
    finally:
        output.close()


def stream(
    tuner: DyUd200,
    channel: Channel,
    pipeline: Pipeline,
    output: Output,
    args: argparse.Namespace,
    stop: threading.Event,
) -> int:
    log.info("tuning to %s", channel)
    tuner.tune(channel.frequency_khz)
    if args.lock_timeout > 0:
        status = tuner.wait_lock(args.lock_timeout)
        if status <= LOCK_THRESHOLD:
            log.error("cannot lock %s (status %d)", channel, status)
            return 2
    purge(tuner, PURGE_PACKETS)
    if stop.is_set():
        return 0

    reader = TsReader(tuner, stop)
    aligner = PacketAligner()
    analyzer = TsAnalyzer()

    def handle(chunk: bytes) -> None:
        packets = aligner.feed(chunk)
        if not packets:
            return
        analyzer.update(packets)
        if not args.keep_null:
            packets = drop_null_packets(packets)
        data = pipeline.process(packets)
        if data:
            output.write(data)

    started = time.monotonic()
    deadline = started + args.rectime if args.rectime is not None else None
    reader.start()
    log.info("recording%s", f" for {args.rectime:g}s" if deadline else "")

    try:
        while not stop.is_set():
            timeout = 0.5
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                timeout = min(timeout, remaining)
            try:
                handle(reader.queue.get(timeout=timeout))
            except queue.Empty:
                continue
        # Everything read before the deadline / signal is still written out.
        reader.halt()
        for chunk in reader.drain():
            handle(chunk)
        tail = pipeline.finish()
        if tail:
            output.write(tail)
    except BrokenPipeError:
        log.debug("output closed")
        return 0
    finally:
        reader.halt()

    if reader.error is not None:
        log.error("TS read failed: %s", reader.error)
        return 1

    log.debug("longest gap between USB reads: %.1f ms", reader.max_gap * 1000)
    t = analyzer.total
    log.info(
        "recorded %.1fs, %.1f MiB, %d packets (TEI %d, CC errors %d, sync losses %d, dropped chunks %d)",
        time.monotonic() - started,
        output.bytes / 1048576,
        t.packets,
        t.tei,
        t.cc_errors,
        aligner.sync_losses,
        reader.overflows,
    )
    if pipeline.descrambler is not None:
        for prog in pipeline.descrambler.programs():
            if prog.undecrypted_packet_count:
                log.warning(
                    "service %d: %d of %d packets were not descrambled (last ECM error %#06x)",
                    prog.program_number,
                    prog.undecrypted_packet_count,
                    prog.total_packet_count,
                    prog.last_ecm_error_code,
                )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose, args.quiet)
    try:
        return record(args)
    except InvalidChannel as e:
        log.error("%s", e)
        return 2
    except DeviceError as e:
        log.error("%s", e)
        return 1
    except BrokenPipeError:
        return 0
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
