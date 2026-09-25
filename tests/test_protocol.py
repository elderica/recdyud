import zlib

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from recdyud.protocol import DEFAULT_KEY, CommandCodec, ProtocolError, t1_block

# Test vector shipped with BonDriver_dyud (dy-ud200-tools.h): a response
# encrypted with the default key.
ENCRYPTED = bytes.fromhex(
    "368aca88eb541c32c9549c3a6ef3fdc29b5638f45124d7d0791532f552391414"
    "ed3408e46be8dca43e8c6af8e7ef5f8425652dec7f0deb405138d9e11dfe2417"
    "a400f01cf04289aa9bd1018381650d03cc737d32cf3b2c9b0973f3ba8662ddcb"
    "cc737d32cf3b2c9b0973f3ba8662ddcba8f42bb47a4039b50e771944870c8bbe"
)
DECRYPTED_HEAD = bytes.fromhex("001c238dc0d00000000001706ba30007")


def _aes_decrypt(key: bytes, data: bytes) -> bytes:
    d = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    return d.update(data) + d.finalize()


def test_decode_bondriver_test_vector():
    plain = CommandCodec().decode(ENCRYPTED)
    assert plain[:16] == DECRYPTED_HEAD


def test_decode_rejects_bad_crc():
    broken = bytearray(ENCRYPTED)
    broken[0] ^= 1
    try:
        CommandCodec().decode(bytes(broken))
    except ProtocolError:
        return
    raise AssertionError("expected ProtocolError")


def test_encode_init_switches_command_key():
    codec = CommandCodec()
    init = bytes.fromhex("0ff0e02000000000238dc0d00ed03875e838dfeb337d3952e378ebd1b784ef65009a0f3a")
    frame = codec.encode(init)
    assert len(frame) == 132
    assert frame[:2] == init[:2]
    plain = bytearray(frame)
    plain[2:130] = _aes_decrypt(DEFAULT_KEY, frame[2:130])
    assert plain[: len(init)] == init
    assert int.from_bytes(plain[128:132], "big") == zlib.crc32(plain[:128])

    # The following 0x1f command is encrypted with bytes 16..31 of the init command.
    cmd = bytes([0x1F, 0xF0, 0xB5, 0x04, 0, 0, 0, 0])
    frame = codec.encode(cmd)
    body = _aes_decrypt(init[16:32], frame[2:130])
    assert body[:6] == cmd[2:]


def test_t1_block_matches_bondriver_commands():
    # szCmdResetBCASC (INT) and szCmdGetBCASId (IDI) of BonDriver_dyud.
    int_cmd = bytes.fromhex("1ff0f00e02003100090000059030000000a5")
    idi_cmd = bytes.fromhex("1ff0f00e02003100090040059032000000e7")
    assert t1_block(bytes([0x90, 0x30, 0, 0, 0]), 0x00) == int_cmd
    assert t1_block(bytes([0x90, 0x32, 0, 0, 0]), 0x40) == idi_cmd
