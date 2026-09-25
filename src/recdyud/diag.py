"""recdyud-diag - diagnostics for the DY-UD200.

* ``list``     list connected tuners
* ``info``     firmware / serial number / B-CAS card status
* ``monitor``  live lock status, signal level and TS errors (for antenna adjustment)
* ``scan``     scan channels and print a channel list (optionally for mirakc)
"""

import argparse
import array
import json
import logging
import queue
import sys
import threading
import time
import unicodedata
from dataclasses import asdict, dataclass

from .bcas import BCasCard, CardError
from .channels import Channel, InvalidChannel, expand_channel_ranges, parse_channel
from .cli import install_signal_handlers, package_version, setup_logging
from .device import LOCK_THRESHOLD, TS_READ_SIZE, DeviceBusy, DeviceError, DyUd200, enumerate_devices, open_device
from .stream import TsReader, purge
from .ts import PacketAligner, SiCollector, TransportInfo, TsAnalyzer, TsCounters

log = logging.getLogger("recdyud")


# ---------------------------------------------------------------------------
# list / info
# ---------------------------------------------------------------------------


def cmd_list(_args: argparse.Namespace) -> int:
    devices = enumerate_devices()
    if not devices:
        print("no DY-UD200 found")
        return 1
    for i, (loc, dev) in enumerate(devices):
        tuner = DyUd200(dev, loc)
        try:
            tuner._claim()
            state = "free"
        except DeviceBusy:
            state = "in use"
        except DeviceError as e:
            state = f"error: {e}"
        finally:
            tuner.release()
        print(f"[{i}] {loc}  {state}")
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    with open_device(args.device) as tuner:
        fw = tuner.firmware
        print(f"Device       : {tuner.location}")
        print(f"Firmware     : {fw}{'' if fw and fw.known else '  (untested)'}")
        print(f"Serial       : {tuner.serial}")
        card = BCasCard(tuner)
        try:
            status = card.initialize()
        except (CardError, DeviceError) as e:
            print(f"B-CAS card   : NG ({e})")
            return 1
        print(f"B-CAS card   : OK (CA system ID {status.ca_system_id:#06x}, status {status.card_status:#06x})")
        for cid in card.ids:
            print(f"Card number  : {cid.number if args.show_card_id else cid.masked}")
    return 0


# ---------------------------------------------------------------------------
# monitor
# ---------------------------------------------------------------------------


def _format_services(info: TransportInfo) -> str:
    parts = []
    for sid in sorted(info.services):
        s = info.services[sid]
        parts.append(f"{sid} {s.name}")
    return ", ".join(parts)


def _describe_ts(info: TransportInfo) -> str:
    name = info.ts_name or info.network_name
    tsid = f"TSID {info.transport_stream_id:#06x}" if info.transport_stream_id is not None else "TSID ?"
    rc = f" / remote key {info.remote_control_key_id}" if info.remote_control_key_id is not None else ""
    return f"{tsid} {name}{rc}: {_format_services(info)}"


def _verdict(lock: int, delta: TsCounters) -> str:
    if lock <= LOCK_THRESHOLD:
        return "NG: no lock"
    if delta.packets == 0:
        return "NG: no data"
    if delta.tei_percent >= 90.0:
        # Typically only the robust one-seg layer (QPSK) is demodulated.
        return "NG: full-seg layer lost"
    if delta.tei:
        return "NG: TEI"
    if delta.cc_errors:
        return "NG: CC"
    return "OK"


def cmd_monitor(args: argparse.Namespace) -> int:
    channel = parse_channel(args.channel)
    stop = install_signal_handlers()
    with open_device(args.device) as tuner:
        if not args.json:
            print(f"# DY-UD200 {tuner.location}, firmware {tuner.firmware}")
            print(f"# {channel}; press Ctrl+C to stop")
        tuner.tune(channel.frequency_khz)
        tuner.wait_lock(args.lock_timeout)

        reader = TsReader(tuner, stop)
        reader.start()
        aligner, analyzer, si = PacketAligner(), TsAnalyzer(), SiCollector()
        started = last = time.monotonic()
        next_report = started + args.interval
        prev = analyzer.total.copy()
        received = 0
        si_printed = False
        try:
            while not stop.is_set():
                try:
                    chunk = reader.queue.get(timeout=0.05)
                except queue.Empty:
                    chunk = None
                if chunk:
                    received += len(chunk)
                    packets = aligner.feed(chunk)
                    analyzer.update(packets)
                    si.update(packets)
                now = time.monotonic()
                if now < next_report:
                    continue

                lock = tuner.get_lock_status()
                level = tuner.get_signal_level()
                delta = analyzer.total - prev
                prev = analyzer.total.copy()
                mbps = received * 8 / (now - last) / 1e6
                received, last = 0, now
                total = analyzer.total
                verdict = _verdict(lock, delta)
                if args.json:
                    record = {
                        "time": round(now - started, 3),
                        "lock": lock,
                        "locked": lock > LOCK_THRESHOLD,
                        "signal": round(level.value, 3),
                        "signal_raw": level.raw,
                        "mbps": round(mbps, 3),
                        "packets": delta.packets,
                        "tei": delta.tei,
                        "tei_percent": round(delta.tei_percent, 2),
                        "cc_errors": delta.cc_errors,
                        "tei_total": total.tei,
                        "cc_errors_total": total.cc_errors,
                        "sync_losses": aligner.sync_losses,
                        "usb_errors": tuner.ts_errors,
                        "scrambled_percent": round(delta.scrambled_percent, 1),
                        "verdict": verdict,
                    }
                    print(json.dumps(record, ensure_ascii=False), flush=True)
                else:
                    print(
                        f"{time.strftime('%H:%M:%S')}  lock {lock:3d}  signal {level.value:7.3f}  "
                        f"{mbps:6.2f} Mbps  TEI {delta.tei_percent:6.2f}% ({delta.tei}, total {total.tei})  "
                        f"CC {delta.cc_errors:4d} (total {total.cc_errors})  "
                        f"scrambled {delta.scrambled_percent:5.1f}%  [{verdict}]",
                        flush=True,
                    )
                    if not si_printed and si.info.complete:
                        print(f"# {_describe_ts(si.info)}", flush=True)
                        si_printed = True
                next_report += args.interval
                if next_report < now:
                    next_report = now + args.interval
                if args.duration and now - started >= args.duration:
                    break
        finally:
            reader.halt()
        if reader.error is not None:
            log.error("TS read failed: %s", reader.error)
            return 1
    return 0


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------


@dataclass
class ScanResult:
    channel: str
    frequency_khz: int
    lock: int
    signal: float
    packets: int = 0
    tei: int = 0
    tei_percent: float = 0.0
    cc_errors: int = 0
    transport_stream_id: int | None = None
    network_name: str = ""
    ts_name: str = ""
    remote_control_key_id: int | None = None
    services: list[dict] | None = None

    @property
    def locked(self) -> bool:
        return self.lock > LOCK_THRESHOLD

    @property
    def name(self) -> str:
        return self.ts_name or self.network_name

    @property
    def received(self) -> bool:
        return self.locked and bool(self.services)


def scan_channel(tuner: DyUd200, ch: Channel, args: argparse.Namespace, stop: threading.Event) -> ScanResult:
    tuner.tune(ch.frequency_khz)
    lock = tuner.wait_lock(args.lock_timeout)
    level = tuner.get_signal_level()
    result = ScanResult(ch.name, ch.frequency_khz, lock, level.value)
    if lock <= LOCK_THRESHOLD:
        return result

    purge(tuner, 512)
    aligner, analyzer, si = PacketAligner(), TsAnalyzer(), SiCollector()
    buf = array.array("B", bytes(TS_READ_SIZE))
    started = time.monotonic()
    while not stop.is_set():
        elapsed = time.monotonic() - started
        if elapsed >= args.dwell or (si.info.complete and elapsed >= args.min_dwell):
            break
        n = tuner.read_ts(buf, timeout=500)
        if n:
            packets = aligner.feed(buf[:n].tobytes())
            analyzer.update(packets)
            si.update(packets)

    # The level reported right after tuning has not settled yet.
    samples = []
    for _ in range(3):
        samples.append(tuner.get_signal_level().value)
        time.sleep(0.1)
    info = si.info
    result.signal = sum(samples) / len(samples)
    result.lock = tuner.get_lock_status()
    result.packets = analyzer.total.packets
    result.tei = analyzer.total.tei
    result.tei_percent = round(analyzer.total.tei_percent, 2)
    result.cc_errors = analyzer.total.cc_errors
    result.transport_stream_id = info.transport_stream_id
    result.network_name = info.network_name
    result.ts_name = info.ts_name
    result.remote_control_key_id = info.remote_control_key_id
    result.services = [
        {"service_id": s.service_id, "service_type": s.service_type, "name": s.name}
        for s in sorted(info.services.values(), key=lambda s: s.service_id)
    ]
    return result


def _yaml_str(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def cmd_scan(args: argparse.Namespace) -> int:
    channels = expand_channel_ranges(args.channels)
    stop = install_signal_handlers()
    results: list[ScanResult] = []
    with open_device(args.device) as tuner:
        print(f"# DY-UD200 {tuner.location}, scanning {len(channels)} channels", file=sys.stderr)
        for ch in channels:
            if stop.is_set():
                break
            r = scan_channel(tuner, ch, args, stop)
            results.append(r)
            if args.json:
                continue
            head = f"{r.channel:>4}  {r.frequency_khz / 1000:8.3f} MHz  lock {r.lock:3d}  signal {r.signal:7.3f}"
            if r.locked:
                tsid = f"{r.transport_stream_id:#06x}" if r.transport_stream_id is not None else "?"
                services = ", ".join(f"{s['service_id']} {s['name']}" for s in r.services or [])
                print(
                    f"{head}  TEI {r.tei_percent:6.2f}%  CC {r.cc_errors:4d}  TSID {tsid}  {r.name}  [{services}]",
                    flush=True,
                )
            elif args.all:
                print(f"{head}  -", flush=True)

    found = [r for r in results if r.received]
    if args.json:
        print(json.dumps([asdict(r) for r in results], ensure_ascii=False, indent=2))
    elif args.mirakc:
        print()
        print("# mirakc config.yml")
        print("channels:")
        for r in found:
            name = unicodedata.normalize("NFKC", r.name or r.channel)
            note = f"  # TEI {r.tei_percent:.1f}%: poor reception" if r.tei_percent >= 1.0 else ""
            print(f"  - name: {_yaml_str(name)}{note}")
            print("    type: GR")
            print(f"    channel: '{r.channel}'")
    if not args.json:
        print(f"# {len(found)} of {len(results)} channels received", file=sys.stderr)
    return 0 if found else 1


# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="recdyud-diag", description="Diagnostics for the DY-UD200 tuner.")
    p.add_argument("-v", "--verbose", action="count", default=0)
    p.add_argument("--version", action="version", version=f"%(prog)s {package_version()}")
    sub = p.add_subparsers(dest="command", required=True)

    def device_arg(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--device", metavar="DEV", help="tuner selector (see recdyud --help)")

    sp = sub.add_parser("list", help="list connected tuners")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("info", help="show firmware, serial number and B-CAS card status")
    device_arg(sp)
    sp.add_argument("--show-card-id", action="store_true", help="show the full B-CAS card number")
    sp.set_defaults(func=cmd_info)

    sp = sub.add_parser("monitor", help="monitor signal and TS errors (antenna adjustment)")
    device_arg(sp)
    sp.add_argument("channel")
    sp.add_argument("--interval", type=float, default=1.0, metavar="SEC", help="report interval (default: 1)")
    sp.add_argument("--duration", type=float, default=0, metavar="SEC", help="stop after SEC seconds")
    sp.add_argument("--lock-timeout", type=float, default=5.0, metavar="SEC")
    sp.add_argument("--json", action="store_true", help="print one JSON object per interval")
    sp.set_defaults(func=cmd_monitor)

    sp = sub.add_parser("scan", help="scan channels")
    device_arg(sp)
    sp.add_argument("--channels", default="13-62", help="channels to scan (default: 13-62), e.g. 13-62,C13-C63")
    sp.add_argument("--lock-timeout", type=float, default=2.0, metavar="SEC")
    sp.add_argument("--dwell", type=float, default=6.0, metavar="SEC", help="max seconds to read SI per channel")
    sp.add_argument("--min-dwell", type=float, default=1.0, metavar="SEC", help="min seconds to count errors")
    sp.add_argument("--all", action="store_true", help="also print channels that did not lock")
    fmt = sp.add_mutually_exclusive_group()
    fmt.add_argument("--mirakc", action="store_true", help="print a channels section for mirakc")
    fmt.add_argument("--json", action="store_true", help="print the result as JSON")
    sp.set_defaults(func=cmd_scan)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose, quiet=False)
    try:
        return args.func(args)
    except (InvalidChannel, DeviceError) as e:
        log.error("%s", e)
        return 1
    except BrokenPipeError:
        return 0
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
