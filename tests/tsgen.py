"""Helpers that build synthetic (optionally MULTI2-scrambled) transport streams."""

from recdyud.ts import PACKET_SIZE, crc32_mpeg2

M32 = 0xFFFFFFFF


# -- MULTI2 (reference implementation, encryption only) ------------------------


def _rot(x: int, n: int) -> int:
    return ((x << n) | (x >> (32 - n))) & M32


def _pi1(l, r):
    return l, r ^ l


def _pi2(l, r, k1):
    y = (r + k1) & M32
    z = (_rot(y, 1) + y - 1) & M32
    return l ^ _rot(z, 4) ^ z, r


def _pi3(l, r, k2, k3):
    y = (l + k2) & M32
    z = (_rot(y, 2) + y + 1) & M32
    a = _rot(z, 8) ^ z
    b = (a + k3) & M32
    c = (_rot(b, 1) - b) & M32
    return l, r ^ _rot(c, 16) ^ (c | l)


def _pi4(l, r, k4):
    y = (r + k4) & M32
    return l ^ ((_rot(y, 2) + y + 1) & M32), r


def _words(b: bytes) -> list[int]:
    return [int.from_bytes(b[i : i + 4], "big") for i in range(0, len(b), 4)]


def multi2_schedule(data_key: bytes, system_key: bytes) -> list[int]:
    sk = _words(system_key)
    dk = _words(data_key)
    a0 = _pi1(dk[0], dk[1])
    a1 = _pi2(*a0, sk[0])
    a2 = _pi3(*a1, sk[1], sk[2])
    a3 = _pi4(*a2, sk[3])
    a4 = _pi1(*a3)
    a5 = _pi2(*a4, sk[4])
    a6 = _pi3(*a5, sk[5], sk[6])
    a7 = _pi4(*a6, sk[7])
    a8 = _pi1(*a7)
    return [a1[0], a2[1], a3[0], a4[1], a5[0], a6[1], a7[0], a8[1]]


def _encrypt_block(l, r, wk, rounds):
    for _ in range(rounds):
        l, r = _pi1(l, r)
        l, r = _pi2(l, r, wk[0])
        l, r = _pi3(l, r, wk[1], wk[2])
        l, r = _pi4(l, r, wk[3])
        l, r = _pi1(l, r)
        l, r = _pi2(l, r, wk[4])
        l, r = _pi3(l, r, wk[5], wk[6])
        l, r = _pi4(l, r, wk[7])
    return l, r


def multi2_encrypt(data: bytes, work_key: list[int], iv: bytes, rounds: int = 4) -> bytes:
    """CBC for whole blocks, OFB for the trailing partial block (ARIB STD-B25)."""
    sl, sr = _words(iv)
    out = bytearray()
    full = len(data) // 8 * 8
    for i in range(0, full, 8):
        pl, pr = _words(data[i : i + 8])
        sl, sr = _encrypt_block(pl ^ sl, pr ^ sr, work_key, rounds)
        out += sl.to_bytes(4, "big") + sr.to_bytes(4, "big")
    if full < len(data):
        kl, kr = _encrypt_block(sl, sr, work_key, rounds)
        ks = kl.to_bytes(4, "big") + kr.to_bytes(4, "big")
        out += bytes(a ^ b for a, b in zip(data[full:], ks, strict=False))
    return bytes(out)


# -- PSI -------------------------------------------------------------------------


def psi_section(table_id: int, ext: int, body: bytes, version: int = 0) -> bytes:
    length = 5 + len(body) + 4
    sec = bytes([table_id, 0xB0 | (length >> 8), length & 0xFF]) + ext.to_bytes(2, "big")
    sec += bytes([0xC1 | (version << 1), 0x00, 0x00]) + body
    return sec + crc32_mpeg2(sec).to_bytes(4, "big")


def pat(tsid: int, programs: dict[int, int]) -> bytes:
    body = b"".join(n.to_bytes(2, "big") + (0xE000 | pid).to_bytes(2, "big") for n, pid in programs.items())
    return psi_section(0x00, tsid, body)


def ca_descriptor(ca_system_id: int, ca_pid: int) -> bytes:
    return bytes([0x09, 4]) + ca_system_id.to_bytes(2, "big") + (0xE000 | ca_pid).to_bytes(2, "big")


def pmt(program: int, pcr_pid: int, program_descriptors: bytes, streams: list[tuple[int, int]]) -> bytes:
    body = (0xE000 | pcr_pid).to_bytes(2, "big") + (0xF000 | len(program_descriptors)).to_bytes(2, "big")
    body += program_descriptors
    for stype, pid in streams:
        body += bytes([stype]) + (0xE000 | pid).to_bytes(2, "big") + (0xF000).to_bytes(2, "big")
    return psi_section(0x02, program, body)


def section_packets(pid: int, section: bytes, cc: int = 0) -> list[bytes]:
    payload = b"\x00" + section
    packets = []
    first = True
    while payload:
        chunk, payload = payload[:184], payload[184:]
        chunk += b"\xff" * (184 - len(chunk))
        header = bytes([0x47, (0x40 if first else 0) | (pid >> 8), pid & 0xFF, 0x10 | (cc & 0x0F)])
        packets.append(header + chunk)
        cc += 1
        first = False
    return packets


def payload_packet(pid: int, cc: int, payload: bytes, *, scrambling: int = 0, adaptation: int = 0) -> bytes:
    """A packet with ``adaptation`` bytes of adaptation field (0 = none)."""
    if adaptation:
        afc = 0x30
        af = bytes([adaptation - 1]) + (b"\x00" + b"\xff" * (adaptation - 2) if adaptation > 1 else b"")
    else:
        afc = 0x10
        af = b""
    body = af + payload
    assert len(body) == 184, len(body)
    return bytes([0x47, pid >> 8, pid & 0xFF, (scrambling << 6) | afc | (cc & 0x0F)]) + body


def null_packet() -> bytes:
    return bytes([0x47, 0x1F, 0xFF, 0x10]) + b"\xff" * 184


assert PACKET_SIZE == 188
