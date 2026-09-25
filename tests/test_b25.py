import random

from tsgen import (
    ca_descriptor,
    multi2_encrypt,
    multi2_schedule,
    null_packet,
    pat,
    payload_packet,
    pmt,
    psi_section,
    section_packets,
)

from recdyud.b25 import Descrambler
from recdyud.bcas import CardId, InitStatus
from recdyud.cli import Pipeline, drop_null_packets

SYSTEM_KEY = bytes(random.Random(1).randrange(256) for _ in range(32))
INIT_CBC = bytes(random.Random(2).randrange(256) for _ in range(8))
KS_ODD = bytes.fromhex("0123456789abcdef")
KS_EVEN = bytes.fromhex("fedcba9876543210")
CA_SYSTEM_ID = 0x0005
PMT_PID, ECM_PID, VIDEO_PID = 0x1F0, 0x1F1, 0x111


class FakeCard:
    def __init__(self):
        self.ecms = []

    def process_ecm(self, ecm):
        self.ecms.append(ecm)
        return KS_ODD + KS_EVEN, 0x0800

    def process_emm(self, emm):
        pass


def status():
    return InitStatus(SYSTEM_KEY, INIT_CBC, 0x123456789A, 0, CA_SYSTEM_ID)


def build_stream(count=300):
    """Returns (scrambled TS, list of expected descrambled video packets)."""
    ecm_body = bytes(range(40))
    head = section_packets(0x00, pat(0x7FE0, {1024: PMT_PID}))
    head += section_packets(PMT_PID, pmt(1024, VIDEO_PID, ca_descriptor(CA_SYSTEM_ID, ECM_PID), [(0x02, VIDEO_PID)]))
    head += section_packets(ECM_PID, psi_section(0x82, 0, ecm_body))

    rng = random.Random(3)
    wk_even = multi2_schedule(KS_EVEN, SYSTEM_KEY)
    wk_odd = multi2_schedule(KS_ODD, SYSTEM_KEY)
    scrambled, expected = [], []
    for i in range(count):
        adaptation = 11 if i % 7 == 0 else 0  # exercise the OFB tail (173 bytes)
        payload = bytes(rng.randrange(256) for _ in range(184 - adaptation))
        even = (i // 50) % 2 == 0
        key = wk_even if even else wk_odd
        enc = multi2_encrypt(payload, key, INIT_CBC)
        scrambled.append(payload_packet(VIDEO_PID, i, enc, scrambling=2 if even else 3, adaptation=adaptation))
        expected.append(payload_packet(VIDEO_PID, i, payload, adaptation=adaptation))
        if i % 40 == 0:
            scrambled.append(null_packet())
    return b"".join(head + scrambled), expected


def video_packets(ts: bytes):
    pkts = [ts[i : i + 188] for i in range(0, len(ts), 188)]
    return [p for p in pkts if ((p[1] & 0x1F) << 8 | p[2]) == VIDEO_PID]


def run(descrambler, ts, chunk=188 * 37):
    out = b""
    for i in range(0, len(ts), chunk):
        descrambler.put(ts[i : i + chunk])
        out += descrambler.get()
    return out + descrambler.flush()


def test_descrambler_roundtrip():
    ts, expected = build_stream()
    card = FakeCard()
    with Descrambler(card, status(), [CardId(0x123456789A, 0)]) as d:
        out = run(d, ts)
        programs = d.programs()
    assert video_packets(out) == expected
    assert card.ecms == [bytes(range(40))]
    assert programs[0].program_number == 1024
    assert programs[0].undecrypted_packet_count == 0


def test_descrambler_strip_removes_null_packets():
    ts, expected = build_stream(100)
    with Descrambler(FakeCard(), status(), [], strip=True) as d:
        out = run(d, ts)
    assert video_packets(out) == expected
    assert all(((out[i + 1] & 0x1F) << 8 | out[i + 2]) != 0x1FFF for i in range(0, len(out), 188))


def test_pipeline_falls_back_when_the_card_fails():
    class BrokenCard(FakeCard):
        def process_ecm(self, ecm):
            raise RuntimeError("card removed")

    ts, _ = build_stream(100)
    pipeline = Pipeline(Descrambler(BrokenCard(), status(), []))
    out = b""
    for i in range(0, len(ts), 188 * 50):
        out += pipeline.process(ts[i : i + 188 * 50])
    out += pipeline.finish()
    # Nothing is lost: the stream is passed through unmodified.
    assert pipeline.descrambler is None
    assert out == ts


def test_drop_null_packets():
    ts = null_packet() + payload_packet(0x100, 0, b"\0" * 184) + null_packet()
    assert drop_null_packets(ts) == payload_packet(0x100, 0, b"\0" * 184)


def test_pipeline_fallback_after_key_change_keeps_every_packet():
    class FlakyCard(FakeCard):
        def process_ecm(self, ecm):
            if self.ecms:
                raise RuntimeError("card removed")
            return super().process_ecm(ecm)

    ts, _ = build_stream(400)
    mid = len(ts) // 2 // 188 * 188
    ecm2 = b"".join(section_packets(ECM_PID, psi_section(0x82, 0, bytes(range(1, 41)), version=1), cc=1))
    ts = ts[:mid] + ecm2 + ts[mid:]
    pipeline = Pipeline(Descrambler(FlakyCard(), status(), []))
    out = b""
    for i in range(0, len(ts), 188 * 50):
        out += pipeline.process(ts[i : i + 188 * 50])
    out += pipeline.finish()
    assert pipeline.descrambler is None
    assert len(out) == len(ts)
    # The tail after the failure is passed through unmodified.
    assert out[-188 * 100 :] == ts[-188 * 100 :]


def test_pipeline_start_timeout_passes_the_stream_through():
    now = [0.0]
    # Only a PAT: the descrambler keeps waiting for the PMT.
    ts = b"".join(
        section_packets(0x00, pat(0x7FE0, {1024: PMT_PID})) + [payload_packet(0x100, i, b"\0" * 184) for i in range(16)]
    )
    pipeline = Pipeline(Descrambler(FakeCard(), status(), []), start_timeout=5.0, clock=lambda: now[0])
    out = pipeline.process(ts)
    assert out == b""
    now[0] = 4.9
    assert pipeline.process(ts) == b""
    now[0] = 5.0
    out = pipeline.process(ts)
    assert pipeline.descrambler is None
    assert out == ts * 3
    assert pipeline.process(ts) == ts
