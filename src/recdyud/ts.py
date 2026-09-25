"""MPEG-2 TS helpers: packet alignment, error statistics and PSI/SI parsing."""

from dataclasses import dataclass, field

import numpy as np

from . import aribstr

PACKET_SIZE = 188
SYNC_BYTE = 0x47
NULL_PID = 0x1FFF
PID_PAT = 0x0000
PID_NIT = 0x0010
PID_SDT = 0x0011

# Number of consecutive sync bytes required to (re)acquire packet alignment.
SYNC_CONFIRM = 4


def _find_sync(a: np.ndarray, start: int) -> int:
    span = PACKET_SIZE * (SYNC_CONFIRM - 1)
    end = len(a) - span
    if end <= start:
        return -1
    ok = a[start:end] == SYNC_BYTE
    for k in range(1, SYNC_CONFIRM):
        ok &= a[start + k * PACKET_SIZE : end + k * PACKET_SIZE] == SYNC_BYTE
    hits = np.flatnonzero(ok)
    return start + int(hits[0]) if hits.size else -1


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
        a = np.frombuffer(data, dtype=np.uint8)
        n = len(data)
        pos = 0
        out: list[bytes] = []
        while True:
            if not self.locked:
                found = _find_sync(a, pos)
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
            heads = a[pos : pos + count * PACKET_SIZE : PACKET_SIZE]
            bad = np.flatnonzero(heads != SYNC_BYTE)
            good = count if bad.size == 0 else int(bad[0])
            if good:
                out.append(data[pos : pos + good * PACKET_SIZE])
                pos += good * PACKET_SIZE
            if bad.size == 0:
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
    """Vectorised TEI / continuity counter / scrambling statistics."""

    def __init__(self) -> None:
        self.total = TsCounters()
        self.pid_packets = np.zeros(8192, dtype=np.int64)
        self._last_cc = np.full(8192, -1, dtype=np.int16)

    def update(self, packets: bytes) -> TsCounters:
        n = len(packets) // PACKET_SIZE
        if n == 0:
            return TsCounters()
        a = np.frombuffer(packets, dtype=np.uint8, count=n * PACKET_SIZE).reshape(n, PACKET_SIZE)
        b1, b2, b3 = a[:, 1], a[:, 2], a[:, 3]
        tei = (b1 & 0x80) != 0
        pid = ((b1 & 0x1F).astype(np.int32) << 8) | b2
        null = (pid == NULL_PID) & ~tei
        scrambled = (b3 & 0xC0) != 0
        has_payload = (b3 & 0x10) != 0

        np.add.at(self.pid_packets, pid, 1)

        idx = np.flatnonzero(has_payload & ~null & ~tei)
        cc_errors = 0
        if idx.size:
            p = pid[idx]
            c = (b3[idx] & 0x0F).astype(np.int16)
            order = np.argsort(p, kind="stable")
            p, c = p[order], c[order]
            first = np.ones(p.size, dtype=bool)
            first[1:] = p[1:] != p[:-1]
            prev = np.empty_like(c)
            prev[1:] = c[:-1]
            prev[first] = self._last_cc[p[first]]
            diff = (c - prev) & 0x0F
            # diff == 0 is a (permitted) duplicate packet.
            cc_errors = int(np.count_nonzero((prev >= 0) & (diff != 1) & (diff != 0)))
            last = np.ones(p.size, dtype=bool)
            last[:-1] = p[:-1] != p[1:]
            self._last_cc[p[last]] = c[last]

        delta = TsCounters(
            packets=n,
            tei=int(np.count_nonzero(tei)),
            cc_errors=cc_errors,
            scrambled=int(np.count_nonzero(scrambled & ~null & ~tei)),
            null=int(np.count_nonzero(null)),
        )
        t = self.total
        t.packets += delta.packets
        t.tei += delta.tei
        t.cc_errors += delta.cc_errors
        t.scrambled += delta.scrambled
        t.null += delta.null
        return delta

    def reset_continuity(self) -> None:
        self._last_cc.fill(-1)


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
        if n == 0:
            return
        a = np.frombuffer(packets, dtype=np.uint8, count=n * PACKET_SIZE).reshape(n, PACKET_SIZE)
        pid = ((a[:, 1] & 0x1F).astype(np.int32) << 8) | a[:, 2]
        for i in np.flatnonzero(np.isin(pid, self.PIDS)):
            off = int(i) * PACKET_SIZE
            p = int(pid[i])
            for sec in self._asm[p].push(packets[off : off + PACKET_SIZE]):
                try:
                    self._parsers[p](sec, self.info)
                except IndexError:
                    pass
