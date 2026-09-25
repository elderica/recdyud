"""ctypes binding of libdyudb25 (libaribb25 + the DY-UD200 card shim)."""

import ctypes
import logging
from dataclasses import dataclass
from typing import Protocol

from . import nativelib
from .bcas import CardId, InitStatus

log = logging.getLogger(__name__)

# arib_std_b25_error_code.h
ERROR_NAMES = {
    -1: "INVALID_PARAM",
    -2: "NO_ENOUGH_MEMORY",
    -3: "NON_TS_INPUT_STREAM",
    -4: "NO_PAT_IN_HEAD_16M",
    -5: "NO_PMT_IN_HEAD_32M",
    -6: "NO_ECM_IN_HEAD_32M",
    -7: "EMPTY_B_CAS_CARD",
    -8: "INVALID_B_CAS_STATUS",
    -9: "ECM_PROC_FAILURE",
    -10: "DECRYPT_FAILURE",
    -11: "PAT_PARSE_FAILURE",
    -12: "PMT_PARSE_FAILURE",
    -13: "ECM_PARSE_FAILURE",
    -14: "CAT_PARSE_FAILURE",
    -15: "EMM_PARSE_FAILURE",
    -16: "EMM_PROC_FAILURE",
}

_u8p = ctypes.POINTER(ctypes.c_uint8)
_ECM_FN = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, _u8p, ctypes.c_int32, _u8p, ctypes.POINTER(ctypes.c_uint32))
_EMM_FN = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, _u8p, ctypes.c_int32)


class B25Error(Exception):
    def __init__(self, op: str, code: int) -> None:
        super().__init__(f"{op} failed: {ERROR_NAMES.get(code, code)} ({code})")
        self.code = code


class Card(Protocol):
    def process_ecm(self, ecm: bytes) -> tuple[bytes, int]: ...

    def process_emm(self, emm: bytes) -> None: ...


@dataclass(frozen=True)
class ProgramInfo:
    program_number: int
    ecm_unpurchased_count: int
    last_ecm_error_code: int
    total_packet_count: int
    undecrypted_packet_count: int


_lib: ctypes.CDLL | None = None


def _load() -> ctypes.CDLL:
    global _lib
    if _lib is not None:
        return _lib
    lib = ctypes.CDLL(nativelib.find_library(nativelib.LIBDYUDB25))
    vp = ctypes.c_void_p
    lib.recdyud_b25_create.argtypes = [ctypes.c_int32, ctypes.c_int32, ctypes.c_int32]
    lib.recdyud_b25_create.restype = vp
    lib.recdyud_b25_set_card.argtypes = [
        vp, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int64, ctypes.c_int32, ctypes.c_int32,
        ctypes.POINTER(ctypes.c_int64), ctypes.c_int32, _ECM_FN, _EMM_FN, vp,
    ]  # fmt: skip
    lib.recdyud_b25_set_card.restype = ctypes.c_int
    lib.recdyud_b25_put.argtypes = [vp, ctypes.c_char_p, ctypes.c_uint32]
    lib.recdyud_b25_put.restype = ctypes.c_int
    for name in ("recdyud_b25_get", "recdyud_b25_withdraw"):
        fn = getattr(lib, name)
        fn.argtypes = [vp, ctypes.POINTER(_u8p), ctypes.POINTER(ctypes.c_uint32)]
        fn.restype = ctypes.c_int
    for name in ("recdyud_b25_flush", "recdyud_b25_reset", "recdyud_b25_program_count"):
        fn = getattr(lib, name)
        fn.argtypes = [vp]
        fn.restype = ctypes.c_int
    i32p, i64p = ctypes.POINTER(ctypes.c_int32), ctypes.POINTER(ctypes.c_int64)
    lib.recdyud_b25_program_info.argtypes = [vp, ctypes.c_int32, i32p, i32p, i32p, i64p, i64p]
    lib.recdyud_b25_program_info.restype = ctypes.c_int
    lib.recdyud_b25_destroy.argtypes = [vp]
    lib.recdyud_b25_destroy.restype = None
    _lib = lib
    return lib


class Descrambler:
    """ARIB STD-B25 descrambler fed with raw TS and backed by ``card``."""

    _h = None

    def __init__(
        self,
        card: Card,
        status: InitStatus,
        ids: list[CardId],
        *,
        multi2_round: int = 4,
        strip: bool = False,
        emm: bool = False,
    ) -> None:
        self._lib = _load()
        self._card = card
        self.callback_error: BaseException | None = None
        self._h = self._lib.recdyud_b25_create(multi2_round, int(strip), int(emm))
        if not self._h:
            raise MemoryError("recdyud_b25_create failed")
        # Keep references so that the callbacks outlive the C side.
        self._ecm_cb = _ECM_FN(self._on_ecm)
        self._emm_cb = _EMM_FN(self._on_emm)
        id_array = (ctypes.c_int64 * max(1, len(ids)))(*[c.id48 for c in ids])
        r = self._lib.recdyud_b25_set_card(
            self._h,
            status.system_key,
            status.init_cbc,
            status.card_id,
            status.card_status,
            status.ca_system_id,
            id_array,
            len(ids),
            self._ecm_cb,
            self._emm_cb,
            None,
        )
        if r < 0:
            self.close()
            raise B25Error("set_b_cas_card", r)

    def _on_ecm(self, _user, ecm, length, ks, return_code) -> int:
        try:
            key, code = self._card.process_ecm(ctypes.string_at(ecm, length))
            ctypes.memmove(ks, key, 16)
            return_code[0] = code
            return 0
        except Exception as e:  # must not propagate into C
            log.warning("ECM processing failed: %s", e)
            self.callback_error = e
            return -1

    def _on_emm(self, _user, emm, length) -> int:
        try:
            self._card.process_emm(ctypes.string_at(emm, length))
            return 0
        except Exception as e:
            log.debug("EMM processing failed: %s", e)
            return -1

    def put(self, data: bytes) -> int:
        """Feed TS data.  Returns 0 or a positive warning; raises B25Error on error."""
        r = self._lib.recdyud_b25_put(self._h, data, len(data))
        if r < 0:
            raise B25Error("put", r)
        return r

    def _take(self, fn, op: str) -> bytes:
        ptr = _u8p()
        size = ctypes.c_uint32()
        r = fn(self._h, ctypes.byref(ptr), ctypes.byref(size))
        if r < 0:
            raise B25Error(op, r)
        return ctypes.string_at(ptr, size.value) if size.value and ptr else b""

    def get(self) -> bytes:
        return self._take(self._lib.recdyud_b25_get, "get")

    def withdraw(self) -> bytes:
        """Return data buffered inside the descrambler without processing it."""
        return self._take(self._lib.recdyud_b25_withdraw, "withdraw")

    def flush(self) -> bytes:
        r = self._lib.recdyud_b25_flush(self._h)
        if r < 0:
            raise B25Error("flush", r)
        return self.get()

    def programs(self) -> list[ProgramInfo]:
        n = self._lib.recdyud_b25_program_count(self._h)
        result = []
        for i in range(max(0, n)):
            pn, unp, err = ctypes.c_int32(), ctypes.c_int32(), ctypes.c_int32()
            total, undec = ctypes.c_int64(), ctypes.c_int64()
            refs = [ctypes.byref(x) for x in (pn, unp, err, total, undec)]
            if self._lib.recdyud_b25_program_info(self._h, i, *refs) == 0:
                result.append(ProgramInfo(pn.value, unp.value, err.value, total.value, undec.value))
        return result

    def close(self) -> None:
        if self._h:
            self._lib.recdyud_b25_destroy(self._h)
            self._h = None

    def __enter__(self) -> Descrambler:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()
