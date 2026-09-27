"""MPEG-2 TS helpers: packet alignment, error statistics and PSI/SI parsing."""

from __future__ import annotations

from dataclasses import dataclass, field

from . import aribstr

PACKET_SIZE = 188
SYNC_BYTE = 0x47
NULL_PID = 0x1FFF
PID_PAT = 0x0000
PID_NIT = 0x0010
PID_SDT = 0x0011

# Number of consecutive sync bytes required to (re)acquire packet alignment.
SYNC_CONFIRM = 4


def _find_sync(data: bytes, start: int) -> int:
    end = len(data) - PACKET_SIZE * (SYNC_CONFIRM - 1)
    i = start
    while i < end:
        i = data.find(SYNC_BYTE, i, end)
        if i < 0:
            break
        if all(data[i + k * PACKET_SIZE] == SYNC_BYTE for k in range(1, SYNC_CONFIRM)):
            return i
        i += 1
    return -1


class PacketAligner:
    """Cuts an arbitrary byte stream into whole, sync-aligned TS packets."""

    def __init__(self) -> None:
        self._pending = b""
        self.locked = False
        self.sync_losses = 0
        self.skipped_bytes = 0

    def reset(self) -> None:
        self._pending = b""
        self.locked = False

    def feed(self, chunk: bytes) -> bytes:
        data = self._pending + chunk if self._pending else bytes(chunk)
        n = len(data)
        pos = 0
        out: list[bytes] = []
        while True:
            if not self.locked:
                found = _find_sync(data, pos)
                if found < 0:
                    keep = max(pos, n - PACKET_SIZE * SYNC_CONFIRM)
                    self.skipped_bytes += keep - pos
                    pos = keep
                    break
                self.skipped_bytes += found - pos
                pos = found
                self.locked = True
            count = (n - pos) // PACKET_SIZE
            if count == 0:
                break
            heads = data[pos : pos + count * PACKET_SIZE : PACKET_SIZE]
            if heads.count(SYNC_BYTE) == count:
                good = count
            else:
                good = next(i for i, b in enumerate(heads) if b != SYNC_BYTE)
            if good:
                out.append(data[pos : pos + good * PACKET_SIZE])
                pos += good * PACKET_SIZE
            if good == count:
                break
            self.locked = False
            self.sync_losses += 1
            pos += 1
        self._pending = data[pos:]
        return b"".join(out)


@dataclass
class TsCounters:
    packets: int = 0
    tei: int = 0
    cc_errors: int = 0
    scrambled: int = 0
    null: int = 0

    def __sub__(self, other: TsCounters) -> TsCounters:
        return TsCounters(
            self.packets - other.packets,
            self.tei - other.tei,
            self.cc_errors - other.cc_errors,
            self.scrambled - other.scrambled,
            self.null - other.null,
        )

    def copy(self) -> TsCounters:
        return TsCounters(self.packets, self.tei, self.cc_errors, self.scrambled, self.null)

    @property
    def payload_packets(self) -> int:
        """Packets that are neither null packets nor errored (TEI) packets."""
        return self.packets - self.null - self.tei

    @property
    def tei_percent(self) -> float:
        """Share of errored packets among non-null packets.

        The headers of errored packets are unreliable, so a TEI packet is never
        counted as a null packet.
        """
        return 100.0 * self.tei / max(1, self.packets - self.null)

    @property
    def scrambled_percent(self) -> float:
        return 100.0 * self.scrambled / max(1, self.payload_packets)


class TsAnalyzer:
    """TEI / continuity counter / scrambling statistics.

    A plain per-packet loop: with the ~16 KiB chunks read from the tuner it is
    several times cheaper than vectorising each chunk with NumPy.
    """

    def __init__(self) -> None:
        self.total = TsCounters()
        self.pid_packets = [0] * 8192
        self._last_cc = [-1] * 8192

    def update(self, packets: bytes) -> TsCounters:
        n = len(packets) // PACKET_SIZE
        if n == 0:
            return TsCounters()
        pid_packets = self.pid_packets
        last_cc = self._last_cc
        tei = cc_errors = scrambled = null = 0
        for off in range(0, n * PACKET_SIZE, PACKET_SIZE):
            b1 = packets[off + 1]
            b3 = packets[off + 3]
            pid = ((b1 & 0x1F) << 8) | packets[off + 2]
            pid_packets[pid] += 1
            if b1 & 0x80:
                tei += 1
                continue
            if pid == NULL_PID:
                null += 1
                continue
            if b3 & 0xC0:
                scrambled += 1
            if b3 & 0x10:
                cc = b3 & 0x0F
                prev = last_cc[pid]
                # A difference of 0 is a (permitted) duplicate packet.
                if prev >= 0 and (cc - prev) & 0x0F > 1:
                    cc_errors += 1
                last_cc[pid] = cc

        delta = TsCounters(packets=n, tei=tei, cc_errors=cc_errors, scrambled=scrambled, null=null)
        t = self.total
        t.packets += delta.packets
        t.tei += delta.tei
        t.cc_errors += delta.cc_errors
        t.scrambled += delta.scrambled
        t.null += delta.null
        return delta

    def reset_continuity(self) -> None:
        self._last_cc = [-1] * 8192


# ---------------------------------------------------------------------------
# PSI / SI
# ---------------------------------------------------------------------------

_CRC_TABLE = []
for _i in range(256):
    _c = _i << 24
    for _ in range(8):
        _c = ((_c << 1) ^ 0x04C11DB7) if _c & 0x80000000 else (_c << 1)
    _CRC_TABLE.append(_c & 0xFFFFFFFF)


def crc32_mpeg2(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for b in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _CRC_TABLE[((crc >> 24) ^ b) & 0xFF]
    return crc


class SectionAssembler:
    """Reassembles PSI sections carried on one PID."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._active = False
        self._cc = -1

    def push(self, pkt: bytes) -> list[bytes]:
        if pkt[1] & 0x80:
            return []
        afc = (pkt[3] >> 4) & 0x3
        if not afc & 1:
            return []
        cc = pkt[3] & 0x0F
        if self._cc >= 0 and cc == self._cc:
            return []  # duplicate
        if self._cc >= 0 and cc != (self._cc + 1) & 0x0F:
            self._active = False
            self._buf.clear()
        self._cc = cc
        p = 4
        if afc & 2:
            p += 1 + pkt[4]
        if p >= PACKET_SIZE:
            return []
        sections: list[bytes] = []
        if pkt[1] & 0x40:  # payload_unit_start_indicator
            pointer = pkt[p]
            p += 1
            if self._active:
                self._buf += pkt[p : p + pointer]
                sections.extend(self._drain())
            self._buf = bytearray(pkt[p + pointer :])
            self._active = True
        elif self._active:
            self._buf += pkt[p:]
        else:
            return sections
        sections.extend(self._drain())
        return sections

    def _drain(self) -> list[bytes]:
        out = []
        buf = self._buf
        while len(buf) >= 3 and buf[0] != 0xFF:
            length = 3 + (((buf[1] & 0x0F) << 8) | buf[2])
            if len(buf) < length:
                break
            section = bytes(buf[:length])
            del buf[:length]
            if crc32_mpeg2(section) == 0:
                out.append(section)
        if buf[:1] == b"\xff":
            buf.clear()
            self._active = False
        return out


def _descriptors(data: bytes):
    i = 0
    while i + 2 <= len(data):
        tag, length = data[i], data[i + 1]
        yield tag, data[i + 2 : i + 2 + length]
        i += 2 + length


@dataclass
class ServiceInfo:
    service_id: int
    service_type: int
    name: str
    provider: str = ""


@dataclass
class TransportInfo:
    transport_stream_id: int | None = None
    original_network_id: int | None = None
    network_name: str = ""
    ts_name: str = ""
    remote_control_key_id: int | None = None
    programs: dict[int, int] = field(default_factory=dict)  # service_id -> PMT PID
    services: dict[int, ServiceInfo] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return bool(self.services) and bool(self.network_name or self.ts_name)


def parse_pat(sec: bytes, info: TransportInfo) -> None:
    if sec[0] != 0x00:
        return
    info.transport_stream_id = int.from_bytes(sec[3:5], "big")
    for i in range(8, len(sec) - 4, 4):
        number = int.from_bytes(sec[i : i + 2], "big")
        pid = int.from_bytes(sec[i + 2 : i + 4], "big") & 0x1FFF
        if number != 0:
            info.programs[number] = pid


def parse_sdt(sec: bytes, info: TransportInfo) -> None:
    if sec[0] != 0x42:  # actual TS only
        return
    # PAT is carried on the full-segment layer only; SDT/NIT also reach one-seg receivers.
    if info.transport_stream_id is None:
        info.transport_stream_id = int.from_bytes(sec[3:5], "big")
    info.original_network_id = int.from_bytes(sec[8:10], "big")
    i, end = 11, len(sec) - 4
    while i + 5 <= end:
        service_id = int.from_bytes(sec[i : i + 2], "big")
        loop_len = int.from_bytes(sec[i + 3 : i + 5], "big") & 0x0FFF
        for tag, body in _descriptors(sec[i + 5 : i + 5 + loop_len]):
            if tag == 0x48 and len(body) >= 3:
                stype = body[0]
                plen = body[1]
                provider = aribstr.decode(body[2 : 2 + plen])
                nlen = body[2 + plen] if 2 + plen < len(body) else 0
                name = aribstr.decode(body[3 + plen : 3 + plen + nlen])
                info.services[service_id] = ServiceInfo(service_id, stype, name, provider)
        i += 5 + loop_len


def parse_nit(sec: bytes, info: TransportInfo) -> None:
    if sec[0] != 0x40:  # actual network only
        return
    nd_len = int.from_bytes(sec[8:10], "big") & 0x0FFF
    for tag, body in _descriptors(sec[10 : 10 + nd_len]):
        if tag == 0x40:
            info.network_name = aribstr.decode(body)
    i = 10 + nd_len
    ts_loop_len = int.from_bytes(sec[i : i + 2], "big") & 0x0FFF
    i += 2
    end = min(i + ts_loop_len, len(sec) - 4)
    while i + 6 <= end:
        tsid = int.from_bytes(sec[i : i + 2], "big")
        d_len = int.from_bytes(sec[i + 4 : i + 6], "big") & 0x0FFF
        if info.transport_stream_id in (None, tsid):
            for tag, body in _descriptors(sec[i + 6 : i + 6 + d_len]):
                if tag == 0xCD and len(body) >= 2:  # TS information descriptor
                    info.remote_control_key_id = body[0]
                    name_len = body[1] >> 2
                    info.ts_name = aribstr.decode(body[2 : 2 + name_len])
        i += 6 + d_len


class SiCollector:
    """Collects PAT / NIT / SDT from aligned TS packets."""

    PIDS = (PID_PAT, PID_NIT, PID_SDT)

    def __init__(self) -> None:
        self.info = TransportInfo()
        self._asm = {pid: SectionAssembler() for pid in self.PIDS}
        self._parsers = {PID_PAT: parse_pat, PID_NIT: parse_nit, PID_SDT: parse_sdt}

    def update(self, packets: bytes) -> None:
        n = len(packets) // PACKET_SIZE
        for off in range(0, n * PACKET_SIZE, PACKET_SIZE):
            # PAT, NIT and SDT all have PIDs below 0x100.
            if packets[off + 1] & 0x1F:
                continue
            p = packets[off + 2]
            if p not in self._asm:
                continue
            for sec in self._asm[p].push(packets[off : off + PACKET_SIZE]):
                try:
                    self._parsers[p](sec, self.info)
                except IndexError:
                    pass
