"""Unit tests for packet.parse / packet.format."""
from packet import Packet, parse, format as fmt


def test_roundtrip():
    p = Packet(seq=42, vbat=614, ibat=100, angle=359, t1=512, t2=-3)
    line = fmt(p)
    assert line.endswith("\r\n")
    parsed = parse(line)
    assert parsed == p


def test_comment_and_blank():
    assert parse("") is None
    assert parse("   ") is None
    assert parse("# hello") is None


def test_malformed():
    assert parse("1,2,3") is None
    assert parse("a,b,c,d,e,f") is None
    assert parse("1,2,3,4,5,6,7") is None


def test_strip_crlf():
    p = parse("7,600,90,180,500,510\r\n")
    assert p is not None
    assert p.seq == 7 and p.angle == 180


if __name__ == "__main__":
    import sys
    ok = 0
    fail = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
                ok += 1
            except AssertionError as e:
                print(f"FAIL {name}: {e}")
                fail += 1
    print(f"{ok} passed, {fail} failed")
    sys.exit(1 if fail else 0)
