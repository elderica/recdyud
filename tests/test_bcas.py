from recdyud.bcas import BCasCard, CardId, parse_id_response, parse_init_response


class FakeTransport:
    def __init__(self, responses):
        self.responses = responses
        self.sent = []
        self.reset_called = False

    def bcas_reset(self):
        self.reset_called = True

    def bcas_transmit(self, apdu):
        self.sent.append(apdu)
        return self.responses[apdu[1]]


def int_response() -> bytes:
    r = bytearray(59)
    r[2:4] = b"\x00\x01"  # card status
    r[4:6] = b"\x21\x00"  # return code
    r[6:8] = b"\x00\x05"  # CA system ID
    r[8:14] = (0x123456789A).to_bytes(6, "big")
    r[16:48] = bytes(range(32))
    r[48:56] = bytes(range(100, 108))
    r[57:59] = b"\x90\x00"
    return bytes(r)


def idi_response() -> bytes:
    r = bytearray(7 + 10 + 2)
    r[6] = 1
    r[7 + 2 : 7 + 8] = (0x123456789A).to_bytes(6, "big")
    r[7 + 8 : 7 + 10] = (12345).to_bytes(2, "big")
    return bytes(r)


def test_parse_init_response():
    s = parse_init_response(int_response())
    assert s.system_key == bytes(range(32))
    assert s.init_cbc == bytes(range(100, 108))
    assert s.ca_system_id == 5
    assert s.card_id == 0x123456789A


def test_parse_id_response_and_number():
    (cid,) = parse_id_response(idi_response())
    assert cid == CardId(0x123456789A, 12345)
    digits = f"{0x123456789A * 100000 + 12345:020d}"
    assert cid.number.replace("-", "") == digits
    assert cid.number.count("-") == 4
    assert cid.masked == "XXXX-XXXX-XXXX-XXXX-" + digits[-4:]


def test_card_ecm():
    ecm_resp = bytes(4) + b"\x08\x00" + bytes(range(16)) + b"\x00" + b"\x90\x00"
    t = FakeTransport({0x30: int_response(), 0x32: idi_response(), 0x34: ecm_resp})
    card = BCasCard(t)
    card.initialize()
    assert t.reset_called
    ks, code = card.process_ecm(b"\xaa" * 30)
    assert ks == bytes(range(16))
    assert code == 0x0800
    assert t.sent[-1] == bytes([0x90, 0x34, 0, 0, 30]) + b"\xaa" * 30 + b"\x00"
