"""Minimal decoder for ARIB STD-B24 8-unit character strings used in SI tables.

Only what is needed to display network / service names is implemented:
Kanji (JIS X 0208/0213), alphanumeric, hiragana, katakana and JIS X 0201
katakana sets, locking/single shifts and designations.  Control codes that
only affect presentation are skipped; DRCS and mosaic characters become "〓".
"""

KANJI = 0x42
JIS_KANJI_1 = 0x39
JIS_KANJI_2 = 0x3A
ADDITIONAL_SYMBOLS = 0x3B
ALNUM = 0x4A
HIRAGANA = 0x30
KATAKANA = 0x31
PROP_ALNUM = 0x36
PROP_HIRAGANA = 0x37
PROP_KATAKANA = 0x38
JIS_X0201_KATAKANA = 0x49

TWO_BYTE_SETS = {KANJI, JIS_KANJI_1, JIS_KANJI_2, ADDITIONAL_SYMBOLS}
DRCS = 0x100  # marker for any DRCS set (value is irrelevant)
DRCS_TWO_BYTE = 0x101

GETA = "〓"

_KANA_TAIL = "ゝゞー。「」、・"
_HIRAGANA = "".join(chr(0x3041 + i) for i in range(0x53)) + "   " + _KANA_TAIL
_KATAKANA = "".join(chr(0x30A1 + i) for i in range(0x56)) + _KANA_TAIL.replace("ゝゞ", "ヽヾ")

# C1 control codes and the number of parameter bytes that follow them.
_C1_PARAMS = {0x8B: 1, 0x90: 1, 0x91: 1, 0x93: 1, 0x94: 1, 0x97: 1, 0x98: 1}


def _two_byte(g: int, b1: int, b2: int) -> str:
    if g in (KANJI, JIS_KANJI_1, JIS_KANJI_2):
        if g == JIS_KANJI_2:
            # Plane 2 of JIS X 0213 is encoded with SS3 in EUC-JIS-2004.
            raw = bytes([0x8F, b1 | 0x80, b2 | 0x80])
        else:
            raw = bytes([b1 | 0x80, b2 | 0x80])
        try:
            return raw.decode("euc_jis_2004")
        except UnicodeDecodeError:
            return GETA
    return GETA


def _one_byte(g: int, b: int) -> str:
    if g in (ALNUM, PROP_ALNUM):
        return chr(b)
    if g in (HIRAGANA, PROP_HIRAGANA):
        return _HIRAGANA[b - 0x21]
    if g in (KATAKANA, PROP_KATAKANA):
        return _KATAKANA[b - 0x21]
    if g == JIS_X0201_KATAKANA and b <= 0x5F:
        return chr(0xFF61 + b - 0x21)
    return GETA


def decode(data: bytes) -> str:
    g = [KANJI, ALNUM, HIRAGANA, KATAKANA]
    gl, gr = 0, 2
    single: int | None = None
    out: list[str] = []
    i, n = 0, len(data)

    def is_two_byte(s: int) -> bool:
        return s in TWO_BYTE_SETS or s == DRCS_TWO_BYTE

    while i < n:
        b = data[i]
        if b == 0x1B:  # ESC
            if i + 1 >= n:
                break
            c = data[i + 1]
            i += 2
            match c:
                case 0x6E:
                    gl = 2
                case 0x6F:
                    gl = 3
                case 0x7E:
                    gr = 1
                case 0x7D:
                    gr = 2
                case 0x7C:
                    gr = 3
                case 0x28 | 0x29 | 0x2A | 0x2B:  # 1-byte G set (or DRCS with 0x20)
                    if i < n and data[i] == 0x20:
                        g[c - 0x28] = DRCS
                        i += 2
                    elif i < n:
                        g[c - 0x28] = data[i]
                        i += 1
                case 0x24:  # 2-byte G set
                    if i < n and data[i] in (0x28, 0x29, 0x2A, 0x2B):
                        idx = data[i] - 0x28
                        i += 1
                        if i < n and data[i] == 0x20:
                            g[idx] = DRCS_TWO_BYTE
                            i += 2
                        elif i < n:
                            g[idx] = data[i]
                            i += 1
                    elif i < n:
                        g[0] = data[i]
                        i += 1
            continue
        if b == 0x0F:
            gl = 0
        elif b == 0x0E:
            gl = 1
        elif b == 0x19:
            single = 2
        elif b == 0x1D:
            single = 3
        elif b == 0x20 or b == 0xA0:
            out.append(" ")
        elif b == 0x0D:
            out.append("\n")
        elif b == 0x16:  # PAPF
            i += 1
        elif b == 0x1C:  # APS
            i += 2
        elif b < 0x20 or b == 0x7F or b == 0xFF:
            pass
        elif 0x80 <= b <= 0x9F:
            if b == 0x9B:  # CSI: skip up to the final byte
                i += 1
                while i < n and not 0x40 <= data[i] <= 0x7E:
                    i += 1
            elif b == 0x9D:  # TIME
                i += 2
            else:
                i += _C1_PARAMS.get(b, 0)
        else:
            if b < 0x80:
                gset = g[single] if single is not None else g[gl]
                single = None
                code = b
            else:
                gset = g[gr]
                code = b & 0x7F
            if is_two_byte(gset):
                if i + 1 >= n:
                    break
                out.append(_two_byte(gset, code, data[i + 1] & 0x7F))
                i += 1
            else:
                out.append(_one_byte(gset, code))
        i += 1
    return "".join(out)
