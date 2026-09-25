from tsgen import null_packet, pat, payload_packet, psi_section, section_packets

from recdyud import aribstr
from recdyud.ts import PacketAligner, SiCollector, TsAnalyzer, crc32_mpeg2


def test_crc32_mpeg2_of_valid_section_is_zero():
    assert crc32_mpeg2(pat(0x7FE0, {1024: 0x1F0})) == 0


def test_aligner_skips_garbage_and_resyncs():
    pkts = [payload_packet(0x100, i, bytes([i]) * 184) for i in range(10)]
    stream = b"\x00\x11\x22" + b"".join(pkts[:5]) + b"\x12\x34" + b"".join(pkts[5:])
    a = PacketAligner()
    out = b""
    for i in range(0, len(stream), 100):  # feed in odd-sized chunks
        out += a.feed(stream[i : i + 100])
    # The 4-packet sync confirmation keeps the last packets pending until more data arrives.
    out += a.feed(b"".join(payload_packet(0x100, 10 + i, b"\0" * 184) for i in range(4)))
    got = [out[i : i + 188] for i in range(0, len(out), 188)]
    assert got[:10] == pkts
    assert a.sync_losses == 1


def test_analyzer_counts_tei_cc_and_scrambling():
    pkts = [payload_packet(0x100, cc, b"\0" * 184) for cc in (0, 1, 2, 4, 5)]  # one gap
    pkts.append(payload_packet(0x100, 5, b"\0" * 184))  # duplicate: allowed
    pkts.append(payload_packet(0x101, 7, b"\0" * 184, scrambling=2))
    tei = bytearray(payload_packet(0x102, 0, b"\0" * 184))
    tei[1] |= 0x80
    pkts.append(bytes(tei))
    pkts.append(null_packet())
    an = TsAnalyzer()
    d = an.update(b"".join(pkts))
    assert d.packets == 9
    assert d.cc_errors == 1
    assert d.tei == 1
    assert d.scrambled == 1
    assert d.null == 1
    assert d.tei_percent == 100.0 / 8
    # continuity is tracked across calls
    d = an.update(payload_packet(0x100, 7, b"\0" * 184))
    assert d.cc_errors == 1
    assert an.pid_packets[0x100] == 7


def _kanji(text: str) -> bytes:
    return bytes(b & 0x7F for b in text.encode("euc_jis_2004"))


def test_aribstr_decode():
    # Default G0 is Kanji; GR is hiragana (G2); LS1 (0x0E) switches GL to alphanumerics.
    data = _kanji("ＮＨＫ総合") + b"\x0e1\x0f" + _kanji("・東京") + bytes([0xA2, 0xA4])
    assert aribstr.decode(data) == "ＮＨＫ総合1・東京あい"
    # ESC ( J designates G0 = alphanumeric
    assert aribstr.decode(b"\x1b\x28\x4aABC 123") == "ABC 123"
    # ESC ~ (LS1R) maps G1 (alnum) to GR
    assert aribstr.decode(b"\x1b\x7e" + bytes([0xC1, 0xC2])) == "AB"


def test_si_collector():
    name = _kanji("テスト")
    sdt_body = (0x0004).to_bytes(2, "big") + b"\xff"
    service = bytes([0x01, 0, len(name)]) + name
    desc = bytes([0x48, len(service)]) + service
    sdt_body += (1024).to_bytes(2, "big") + b"\xfc" + (0x8000 | len(desc)).to_bytes(2, "big") + desc
    sdt = psi_section(0x42, 0x7FE0, sdt_body)

    net = _kanji("ネット")
    ts_info = bytes([0xCD, 2 + len(net), 1, len(net) << 2]) + net
    net_desc = bytes([0x40, len(net)]) + net
    ts_loop = (0x7FE0).to_bytes(2, "big") + (0x0004).to_bytes(2, "big")
    ts_loop += (0xF000 | len(ts_info)).to_bytes(2, "big") + ts_info
    nit_body = (0xF000 | len(net_desc)).to_bytes(2, "big") + net_desc
    nit_body += (0xF000 | len(ts_loop)).to_bytes(2, "big") + ts_loop
    nit = psi_section(0x40, 0x7FE0, nit_body)

    stream = b"".join(
        section_packets(0x00, pat(0x7FE0, {1024: 0x1F0})) + section_packets(0x11, sdt) + section_packets(0x10, nit)
    )
    si = SiCollector()
    si.update(stream)
    info = si.info
    assert info.transport_stream_id == 0x7FE0
    assert info.programs == {1024: 0x1F0}
    assert info.services[1024].name == "テスト"
    assert info.network_name == "ネット"
    assert info.ts_name == "ネット"
    assert info.remote_control_key_id == 1
    assert info.complete
