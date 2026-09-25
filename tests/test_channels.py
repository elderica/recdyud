import pytest

from recdyud.channels import InvalidChannel, expand_channel_ranges, parse_channel


@pytest.mark.parametrize(
    ("spec", "name", "khz"),
    [
        ("13", "13", 473_143),
        ("27", "27", 557_143),
        ("62", "62", 767_143),
        ("GR27", "27", 557_143),
        ("C13", "C13", 111_143),
        ("C22", "C22", 167_143),
        ("C23", "C23", 225_143),
        ("C24", "C24", 233_143),
        ("C28", "C28", 255_143),
        ("C63", "C63", 465_143),
        ("1", "1", 93_143),
        ("4", "4", 173_143),
        ("12", "12", 219_143),
        ("473143kHz", "473143kHz", 473_143),
        ("557.143MHz", "557143kHz", 557_143),
    ],
)
def test_parse_channel(spec, name, khz):
    ch = parse_channel(spec)
    assert (ch.name, ch.frequency_khz) == (name, khz)


@pytest.mark.parametrize("spec", ["0", "63", "C12", "C64", "BS15_0", "abc", "10kHz"])
def test_invalid_channel(spec):
    with pytest.raises(InvalidChannel):
        parse_channel(spec)


def test_expand_channel_ranges():
    names = [c.name for c in expand_channel_ranges("13-15,27,C13-C14")]
    assert names == ["13", "14", "15", "27", "C13", "C14"]
