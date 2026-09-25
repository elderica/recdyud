"""B-CAS card access through the card slot of the DY-UD200.

APDUs and response layouts follow ARIB STD-B25 / libaribb25's b_cas_card.c.
"""

import logging
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger(__name__)

CMD_INITIAL_SETTING_CONDITIONS = bytes([0x90, 0x30, 0x00, 0x00, 0x00])
CMD_CARD_ID_INFORMATION = bytes([0x90, 0x32, 0x00, 0x00, 0x00])
ECM_HEADER = bytes([0x90, 0x34, 0x00, 0x00])
EMM_HEADER = bytes([0x90, 0x36, 0x00, 0x00])

# The DY-UD200 can carry at most 115 APDU bytes: header(4) + Lc(1) + data + Le(1).
MAX_ECM_EMM_SIZE = 115 - 6

RETURN_CODE_OK = 0x2100
PURCHASED_RETURN_CODES = {0x0200, 0x0400, 0x0800}


class Transport(Protocol):
    def bcas_reset(self) -> None: ...

    def bcas_transmit(self, apdu: bytes) -> bytes: ...


class CardError(Exception):
    pass


@dataclass(frozen=True)
class InitStatus:
    system_key: bytes
    init_cbc: bytes
    card_id: int
    card_status: int
    ca_system_id: int


@dataclass(frozen=True)
class CardId:
    id48: int
    check_code: int

    @property
    def number(self) -> str:
        """The 20-digit number printed on the card, grouped by 4 digits."""
        digits = f"{self.id48 * 100000 + self.check_code:020d}"
        return "-".join(digits[i : i + 4] for i in range(0, 20, 4))

    @property
    def masked(self) -> str:
        return "XXXX-XXXX-XXXX-XXXX-" + self.number[-4:]


def parse_init_response(r: bytes) -> InitStatus:
    if len(r) < 57:
        raise CardError(f"INT response too short ({len(r)} bytes)")
    code = int.from_bytes(r[4:6], "big")
    if code != RETURN_CODE_OK:
        raise CardError(f"INT returned {code:#06x} (is a B-CAS card inserted?)")
    return InitStatus(
        system_key=bytes(r[16:48]),
        init_cbc=bytes(r[48:56]),
        card_id=int.from_bytes(r[8:14], "big"),
        card_status=int.from_bytes(r[2:4], "big"),
        ca_system_id=int.from_bytes(r[6:8], "big"),
    )


def parse_id_response(r: bytes) -> list[CardId]:
    if len(r) < 19:
        raise CardError(f"IDI response too short ({len(r)} bytes)")
    count = r[6]
    ids = []
    for i in range(count):
        p = 7 + i * 10
        if p + 10 > len(r):
            raise CardError("IDI response truncated")
        ids.append(CardId(int.from_bytes(r[p + 2 : p + 8], "big"), int.from_bytes(r[p + 8 : p + 10], "big")))
    return ids


class BCasCard:
    def __init__(self, transport: Transport) -> None:
        self._t = transport
        self.status: InitStatus | None = None
        self.ids: list[CardId] = []
        self.ecm_count = 0
        self.ecm_errors = 0

    def initialize(self) -> InitStatus:
        self._t.bcas_reset()
        self.status = parse_init_response(self._t.bcas_transmit(CMD_INITIAL_SETTING_CONDITIONS))
        self.ids = parse_id_response(self._t.bcas_transmit(CMD_CARD_ID_INFORMATION))
        return self.status

    def process_ecm(self, ecm: bytes) -> tuple[bytes, int]:
        """Return (Ks odd||even, return code) for an ECM section body."""
        if len(ecm) > MAX_ECM_EMM_SIZE:
            raise CardError(f"ECM too large for DY-UD200 ({len(ecm)} bytes)")
        self.ecm_count += 1
        try:
            r = self._t.bcas_transmit(ECM_HEADER + bytes([len(ecm)]) + ecm + b"\x00")
        except Exception:
            self.ecm_errors += 1
            raise
        if len(r) < 25:
            self.ecm_errors += 1
            raise CardError(f"ECM response too short ({len(r)} bytes)")
        code = int.from_bytes(r[4:6], "big")
        if code not in PURCHASED_RETURN_CODES:
            log.warning("ECM rejected by the card (return code %#06x)", code)
        return bytes(r[6:22]), code

    def process_emm(self, emm: bytes) -> None:
        if len(emm) > MAX_ECM_EMM_SIZE:
            raise CardError(f"EMM too large for DY-UD200 ({len(emm)} bytes)")
        r = self._t.bcas_transmit(EMM_HEADER + bytes([len(emm)]) + emm + b"\x00")
        if len(r) < 6:
            raise CardError(f"EMM response too short ({len(r)} bytes)")
