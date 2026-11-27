import pytest

from semver import bump, compare, parse


@pytest.mark.parametrize("text,canon,pre", [
    ("1.2.3", "1.2.3", ()),
    ("v1.2.3", "1.2.3", ()),
    ("1.2.3-rc.1", "1.2.3-rc.1", ("rc", 1)),
    ("1.2.3+build.5", "1.2.3", ()),
    ("1.2.3-rc.1+build.5", "1.2.3-rc.1", ("rc", 1)),
    ("1.0.0-x-y.7z.0", "1.0.0-x-y.7z.0", ("x-y", "7z", 0)),
])
def test_parse(text, canon, pre):
    v = parse(text)
    assert str(v) == canon and tuple(v.pre) == pre


@pytest.mark.parametrize("bad", ["1.2", "01.2.3", "1.02.3", "1.2.3-rc.01", "1.2.3-", "1.2.3-a..b",
                                 "1.2.3-r$c", "", "1.2.3.4", "a.b.c"])
def test_parse_rejects(bad):
    with pytest.raises(ValueError):
        parse(bad)


ORDER = ["1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta", "1.0.0-beta.2",
         "1.0.0-beta.11", "1.0.0-rc.1", "1.0.0", "1.0.1", "1.1.0", "2.0.0", "10.0.0"]


def test_precedence_chain():
    for a, b in zip(ORDER, ORDER[1:]):
        assert compare(a, b) == -1, (a, b)
        assert compare(b, a) == 1, (b, a)


def test_compare_equal_and_build_ignored():
    assert compare("1.0.0+a", "1.0.0+b") == 0
    assert compare("v1.0.0", "1.0.0") == 0


@pytest.mark.parametrize("v,part,out", [
    ("1.2.3", "major", "2.0.0"), ("1.2.3", "minor", "1.3.0"), ("1.2.3", "patch", "1.2.4"),
    ("1.2.3-rc.1", "patch", "1.2.3"), ("1.3.0-rc", "minor", "1.3.0"), ("2.0.0-rc.1", "major", "2.0.0"),
    ("1.2.3-rc.1", "minor", "1.3.0"), ("1.2.0-rc.1", "major", "2.0.0"), ("1.2.3+b", "patch", "1.2.4"),
])
def test_bump(v, part, out):
    assert bump(v, part) == out


def test_bump_rejects_part():
    with pytest.raises(ValueError):
        bump("1.2.3", "build")
