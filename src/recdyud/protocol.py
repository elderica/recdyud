"""Framing and encryption of DY-UD200 control commands.

Derived from BonDriver_dyud (``SendCommandAndRecvResponse``):

* A command is padded to 128 bytes and followed by a big-endian CRC-32 of
  those 128 bytes (132 bytes in total).
* Bytes 2..129 are encrypted with AES-ECB.  Commands whose first byte has
  bit 4 cleared (the ``0x0f`` initialisation commands) use the fixed AES-256
  key; bytes 16..31 of such a command then become the AES-128 key for the
  following ``0x1f`` commands.
* A response is 128 bytes, AES-256-ECB encrypted with the fixed key, and its
  last 4 bytes are a big-endian CRC-32 of the first 124 bytes.
"""

import zlib

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

COMMAND_SIZE = 128
FRAME_SIZE = COMMAND_SIZE + 4
RESPONSE_SIZE = 128

DEFAULT_KEY = bytes.fromhex("7593ee380d9944959fc7672c59eb1f69") + bytes(16)


class ProtocolError(Exception):
    pass


def _ecb(key: bytes) -> Cipher:
    return Cipher(algorithms.AES(key), modes.ECB())


class CommandCodec:
    def __init__(self) -> None:
        self._default = _ecb(DEFAULT_KEY)
        self._command = _ecb(DEFAULT_KEY[:16])

    def encode(self, command: bytes) -> bytes:
        if len(command) > COMMAND_SIZE:
            raise ValueError(f"command too long: {len(command)} bytes")
        frame = bytearray(FRAME_SIZE)
        frame[: len(command)] = command
        frame[COMMAND_SIZE:] = zlib.crc32(frame[:COMMAND_SIZE]).to_bytes(4, "big")

        if frame[0] & 0x10:
            enc = self._command.encryptor()
        else:
            enc = self._default.encryptor()
            self._command = _ecb(bytes(frame[16:32]))
        body = enc.update(bytes(frame[2:130])) + enc.finalize()
        return bytes(frame[:2]) + body + bytes(frame[130:])

    def decode(self, response: bytes) -> bytes:
        if len(response) < RESPONSE_SIZE:
            raise ProtocolError(f"short response: {len(response)} bytes")
        dec = self._default.decryptor()
        plain = dec.update(bytes(response[:RESPONSE_SIZE])) + dec.finalize()
        if zlib.crc32(plain[:124]) != int.from_bytes(plain[124:128], "big"):
            raise ProtocolError("response CRC mismatch")
        return plain


def t1_block(apdu: bytes, pcb: int) -> bytes:
    """Wrap an APDU into the vendor command that carries a T=1 I-block.

    Layout: ``1f f0 f0 <len+9> 02 00 31 <block length, BE16> NAD PCB LEN INF LRC``
    """
    if len(apdu) > 115:
        raise ValueError(f"APDU too long for DY-UD200: {len(apdu)} bytes (max 115)")
    n = len(apdu)
    block = bytearray([0x00, pcb, n]) + apdu
    lrc = 0
    for b in block:
        lrc ^= b
    block.append(lrc)
    return bytes([0x1F, 0xF0, 0xF0, n + 9, 0x02, 0x00, 0x31]) + (n + 4).to_bytes(2, "big") + bytes(block)
