import json
import os
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "vacuous-test-guard"


def run(tool, tool_input, **env):
    p = subprocess.run([sys.executable, str(HOOK)], input=json.dumps({"tool_name": tool, "tool_input": tool_input}),
                       capture_output=True, text=True, env={**os.environ, **env})
    return p.returncode, p.stderr


def write(tmp_path, name, content, **env):
    f = tmp_path / name
    f.write_text(content)
    return run("Write", {"file_path": str(f), "content": content}, **env)


def edit(tmp_path, name, before, old, new):
    f = tmp_path / name
    f.write_text(before.replace(old, new, 1))
    return run("Edit", {"file_path": str(f), "old_string": old, "new_string": new})


def test_reports_a_python_test_that_asserts_nothing(tmp_path):
    code, err = write(tmp_path, "test_cart.py", "def test_total():\n    total([1, 2])\n")
    assert code == 2 and "asserts nothing: test_total" in err


def test_reports_always_true_assertions_in_each_language(tmp_path):
    cases = {
        "test_a.py": "def test_a():\n    run()\n    assert True\n",
        "a.test.ts": "it('works', () => {\n  run();\n  expect(true).toBe(true);\n});\n",
        "a_test.go": "func TestA(t *testing.T) {\n\tassert.True(t, true)\n}\n",
        "tests/a.rs": "#[test]\nfn a() {\n    assert_eq!(1, 1);\n}\n",
    }
    for name, content in cases.items():
        (tmp_path / "tests").mkdir(exist_ok=True)
        code, err = write(tmp_path, name, content)
        assert code == 2 and "always true" in err, name


def test_quiet_on_real_assertions(tmp_path):
    good = (
        "import pytest\n\n"
        "def check_shape(r):\n    assert r.ok\n\n"
        "def test_value():\n    assert total([1, 2]) == 3\n\n"
        "def test_raises():\n    with pytest.raises(ValueError):\n        total(None)\n\n"
        "def test_helper():\n    check_shape(call())\n\n"
        "def test_mock(m):\n    call(m)\n    m.assert_called_once()\n\n"
        "@pytest.mark.skip(reason='needs network')\ndef test_later():\n    pass\n"
    )
    assert write(tmp_path, "test_good.py", good)[0] == 0
    assert write(tmp_path, "b.test.js", "it('adds', () => { expect(add(1, 2)).toBe(3); });\n")[0] == 0


def test_counts_only_what_the_edit_adds(tmp_path):
    before = "def test_old():\n    run()\n\ndef test_value():\n    assert f() == 1\n"
    code, _ = edit(tmp_path, "test_delta.py", before, "assert f() == 1", "assert f() == 2")
    assert code == 0
    code, err = edit(tmp_path, "test_delta.py", before, "    assert f() == 1", "    f()")
    assert code == 2 and "test_value" in err and "test_old" not in err


def test_ignores_non_test_files_kill_switch_and_bad_input(tmp_path):
    assert write(tmp_path, "cart.py", "def test_total():\n    total()\n")[0] == 0
    assert write(tmp_path, "test_x.py", "def test_total():\n    total()\n", VACUOUS_TEST_GUARD_DISABLE="1")[0] == 0
    assert write(tmp_path, "test_broken.py", "def test_total(:\n")[0] == 0
    p = subprocess.run([sys.executable, str(HOOK)], input="nope", capture_output=True, text=True)
    assert p.returncode == 0
