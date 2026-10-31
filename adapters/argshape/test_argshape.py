"""argshape tests: parser units plus end-to-end learn -> correct loops.

End-to-end cases drive the real hook entry point (`argshape.py hook ...`) as a
subprocess with JSON on stdin, against a throwaway state dir, a real fake CLI
(argparse, so its errors and --help are genuine), and a real sqlite database.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import errclass  # noqa: E402
import helpspec  # noqa: E402
import segment   # noqa: E402
import sqlshape  # noqa: E402

FAKE_CLI = r'''#!/usr/bin/env python3
import argparse, sys
p = argparse.ArgumentParser(prog="fakecli")
p.add_argument("--verbose", action="store_true")
sub = p.add_subparsers(dest="cmd", required=True)
ls = sub.add_parser("list")
ls.add_argument("--limit", type=int, default=10)
ls.add_argument("--format", default="text")
show = sub.add_parser("show")
show.add_argument("name")
a = p.parse_args()
print("ok", a.cmd, getattr(a, "limit", ""))
'''


class SegmentTests(unittest.TestCase):
    def bins(self, cmd):
        return [(s.bin, [w.value for w in s.args]) for s in segment.parse(cmd)]

    def test_pipeline_and_lists(self):
        self.assertEqual(self.bins("cd /x && rg --limt 5 foo | head -3; echo done"),
                         [("cd", ["/x"]), ("rg", ["--limt", "5", "foo"]), ("head", ["-3"]), ("echo", ["done"])])

    def test_wrappers_and_redirects(self):
        segs = segment.parse("FOO=1 timeout 10 git log --onelin -n 3 2>&1 | tail")
        self.assertEqual(segs[0].bin, "git")
        self.assertEqual([w.value for w in segs[0].args], ["log", "--onelin", "-n", "3"])
        self.assertEqual(segs[0].via, ["timeout"])
        self.assertEqual(segs[0].assigns[0].value, "FOO=1")

    def test_dashdash_and_docker_exec(self):
        s = segment.parse("hearth-vault exec --prefix ai/ -- psql -d db -c 'select 1'")[0]
        self.assertEqual((s.bin, s.args[-1].value), ("psql", "select 1"))
        s = segment.parse("docker exec -it -e A=1 pg psql -U postgres -c 'select 1'")[0]
        self.assertEqual(s.bin, "psql")
        self.assertEqual(s.via, ["docker exec pg"])

    def test_heredoc_and_herestring(self):
        segs = segment.parse("psql -d x <<'SQL'\nselect a from b;\nSQL\necho after")
        self.assertEqual(segs[0].stdin_text, "select a from b;\n")
        self.assertEqual(segs[1].bin, "echo")
        segs = segment.parse("cat <<< 'select 1' > out.txt; ls -l>x")
        self.assertEqual(segs[0].stdin_text, "select 1")
        self.assertEqual(self.bins("cat <<< 'x'; ls -l>x")[1], ("ls", ["-l"]))

    def test_spans_are_exact(self):
        cmd = 'echo "a b" && rg --limt 5 x'
        w = segment.parse(cmd)[1].args[0]
        self.assertEqual(cmd[w.start:w.end], "--limt")

    def test_quotes_and_substitution(self):
        segs = segment.parse('x=$(ls -la | grep "a b") && echo "$x" # c')
        self.assertEqual(segs[-1].bin, "echo")
        self.assertTrue(segment.has_optout("rg --limt 1 x # argshape:off"))

    def test_compound_skipped(self):
        self.assertEqual(self.bins("for f in *.py; do wc -l $f; done"), [("wc", ["-l", "$f"])])


class ErrclassTests(unittest.TestCase):
    CASES = {
        "error: unexpected argument '--limt' found\n\n  tip: a similar argument exists: '--limit'\n": ("flag", "--limt", "--limit"),
        "ls: unrecognized option '--colr'\nTry 'ls --help' for more information.": ("flag", "--colr", None),
        "head: invalid option -- 'z'": ("flag", "-z", None),
        "Error: unknown flag: --limt": ("flag", "--limt", None),
        'Error: unknown command "lsit" for "gh repo"\n\nDid you mean this?\n\tlist\n': ("subcmd", "lsit", "list"),
        "flag provided but not defined: -limt": ("flag", "-limt", None),
        "error: unknown option `onelin'": ("flag", "--onelin", None),
        "git: 'stauts' is not a git command. See 'git --help'.\n\nThe most similar command is\n\tstatus": ("subcmd", "stauts", "status"),
        "x: error: unrecognized arguments: --limt 5": ("flag", "--limt", None),
        "Error: No such option: --limt Did you mean --limit?": ("flag", "--limt", "--limit"),
        'ERROR:  column "session_date" does not exist\nLINE 1: x\nHINT:  Perhaps you meant to reference the column "t.trade_date".': ("column", "session_date", "t.trade_date"),
        "Error: in prepare, no such column: nme": ("column", "nme", None),
    }

    def test_cases(self):
        for text, (kind, tok, hint) in self.CASES.items():
            r = errclass.classify(text)
            self.assertTrue(r, text)
            self.assertEqual((r[0].kind, r[0].token, r[0].hint), (kind, tok, hint), text)

    def test_signature_is_stable(self):
        a = errclass.signature("Exit code 2\nls: cannot access '/tmp/a1': No such file")
        b = errclass.signature("Exit code 2\nls: cannot access '/var/b2': No such file")
        self.assertEqual(a, b)


class HelpSpecTests(unittest.TestCase):
    def test_formats(self):
        text = """Usage: x [OPTIONS]
  -n, --limit <LIMIT>     clap style
      --format=FMT        gnu style
  -v, --verbose           flag
  --[no-]color            toggle
  -timeout duration       go flag style
Commands:
  list    List things
  show    Show a thing
"""
        sp = helpspec.parse_help(text)
        self.assertTrue(sp.opts["--limit"])
        self.assertTrue(sp.opts["--format"])
        self.assertFalse(sp.opts["--verbose"])
        self.assertIn("--no-color", sp.opts)
        self.assertIn("-timeout", sp.opts)
        self.assertEqual(sp.subcmds, ["list", "show"])

    def test_unique_fix_is_conservative(self):
        sp = helpspec.Spec({"--limit": True, "--line": False, "--format": True, "-n": True})
        self.assertEqual(helpspec.unique_fix("--limt", sp), "--limit")
        self.assertEqual(helpspec.unique_fix("--form", sp), "--format")
        self.assertEqual(helpspec.unique_fix("-format", sp), "--format")
        self.assertIsNone(helpspec.unique_fix("--li", sp))      # too short / ambiguous
        self.assertIsNone(helpspec.unique_fix("--zzz", sp))

    def test_sqlish_gate(self):
        import argshape
        for txt in ("git commit -m 'fix with tests'", "echo update the docs", "grep select x"):
            self.assertIsNone(argshape.SQLISH.search(txt), txt)
        for txt in ("psql -c 'select a from b'", "sqlite3 x 'insert into t values (1)'"):
            self.assertIsNotNone(argshape.SQLISH.search(txt), txt)

    def test_never_runs_denylisted(self):
        self.assertFalse(helpspec.safe_to_run("/bin/rm", "rm", True))


class ShapeTests(unittest.TestCase):
    def test_mac_variants(self):
        self.assertEqual(sqlshape.shape_of("aa:bb:cc:dd:ee:ff"), ("mac", "colon/lower"))
        self.assertEqual(sqlshape.shape_of("AABB.CCDD.EEFF"), ("mac", "dot4/upper"))
        self.assertEqual(sqlshape.convert("AA-BB-CC-DD-EE-01", "mac", "colon/lower"), "aa:bb:cc:dd:ee:01")
        self.assertEqual(sqlshape.convert("aabbccddee01", "mac", "dot4/upper"), "AABB.CCDD.EE01")
        self.assertIsNone(sqlshape.convert("aa:bb:cc:dd:ee:01", "mac", "colon/lower"))

    def test_uuid_and_mixed(self):
        self.assertEqual(sqlshape.convert("0123456789ABCDEF0123456789ABCDEF", "uuid", "dashed/lower"),
                         "01234567-89ab-cdef-0123-456789abcdef")
        self.assertIsNone(sqlshape.infer_column_shape(["aa:bb:cc:dd:ee:ff", "hello", "x"]))
        self.assertEqual(sqlshape.infer_column_shape(["aa:bb:cc:dd:ee:ff", "00:11:22:33:44:55"]),
                         ("mac", "colon/lower"))

    def test_sql_helpers(self):
        sql = "select nme, t.mac from devices t join x on x.id=t.id where mac = 'AA' and name in ('a','b')"
        self.assertEqual(sqlshape.relations(sql), ["devices", "x"])
        self.assertIn("nme", sqlshape.identifiers(sql))
        self.assertIn(("mac", "AA"), sqlshape.literal_comparisons(sql))
        self.assertEqual(sqlshape.replace_ident("select nme from t where x='nme'", "nme", "name"),
                         "select name from t where x='nme'")


class SchemaTests(unittest.TestCase):
    def test_emitted_payloads_match_schema(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema not installed")
        schema = json.load(open(os.path.join(HERE, "..", "..", "schemas", "agent.cli.correction.v1.json")))
        import core
        sent = []
        orig = core.shutil.which
        core.shutil.which = lambda name: "/bin/true" if name == "nervous" else orig(name)
        orig_popen = core.subprocess.Popen
        core.subprocess.Popen = lambda argv, **kw: sent.append(json.loads(argv[3]))
        os.environ.pop("ARGSHAPE_NO_BUS", None)
        try:
            core.emit({"event": "rejection", "bin": "rg", "ctx": "", "kind": "flag", "bad": "--limt",
                       "good": "", "version": "ripgrep 15"})
            core.emit({"event": "correction", "bin": "rg", "kind": "flag", "bad": "--limt",
                       "good": "--max-count", "source": "spec-unique", "session_id": "s"})
            core.emit({"event": "demotion", "kind": "column", "bad": "a", "good": "b", "reason": "opt-out"})
            core.emit({"event": "breaker", "applied": 4, "bad_count": 3})
        finally:
            core.shutil.which = orig
            core.subprocess.Popen = orig_popen
        self.assertEqual(len(sent), 4)
        for p in sent:
            jsonschema.Draft202012Validator(schema).validate(p)


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="argshape-test-")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        self.cli = os.path.join(self.bin, "fakecli")
        with open(self.cli, "w") as f:
            f.write(FAKE_CLI.replace("#!/usr/bin/env python3", "#!" + sys.executable))
        os.chmod(self.cli, 0o755)
        self.env = {**os.environ, "ARGSHAPE_STATE_DIR": os.path.join(self.tmp, "state"),
                    "ARGSHAPE_CONFIG": os.path.join(self.tmp, "config.json"),
                    "ARGSHAPE_NO_BUS": "1", "PATH": self.bin + ":" + os.environ["PATH"]}
        self.env.pop("ARGSHAPE_MODE", None)
        self.n = 0

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cmd(self, command):
        p = subprocess.run(["/bin/bash", "-c", command], capture_output=True, text=True,
                           env=self.env, cwd=self.tmp)
        return p.returncode, p.stdout, p.stderr

    def hook(self, kind, command, session="s1", tid=None, **extra):
        self.n += 1
        ev = {"session_id": session, "cwd": self.tmp, "tool_name": "Bash",
              "tool_use_id": tid or f"tu{self.n}",
              "tool_input": {"command": command, "description": "d", "timeout": 5000}, **extra}
        p = subprocess.run([sys.executable, os.path.join(HERE, "argshape.py"), "hook", kind],
                           input=json.dumps(ev), capture_output=True, text=True, env=self.env, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)["hookSpecificOutput"] if p.stdout.strip() else None

    def execute(self, command, session="s1"):
        """What Claude Code does: pre hook, run (possibly rewritten), post/fail hook."""
        tid = f"exec{self.n}-{time.time()}"
        pre = self.hook("pre", command, session, tid)
        ran = pre["updatedInput"]["command"] if pre and "updatedInput" in pre else command
        rc, out, err = self.run_cmd(ran)
        if rc == 0:
            post = self.hook("post", ran, session, tid,
                             tool_response={"stdout": out, "stderr": err, "interrupted": False})
        else:
            post = self.hook("fail", ran, session, tid, error=f"Exit code {rc}\n{out}{err}")
        return pre, ran, rc, post

    def cli_cmd(self, *args):
        return [sys.executable, os.path.join(HERE, "argshape.py"), *args]

    def test_learn_then_correct_flag(self):
        pre, ran, rc, post = self.execute("fakecli list --limt 5 | cat; true && fakecli list --limt 5")
        self.assertIsNone(pre)
        self.assertNotEqual(rc, 0)
        self.assertIn("--limit", post["additionalContext"])
        pre, ran, rc, post = self.execute("fakecli list --limt 5")
        self.assertEqual(ran, "fakecli list --limit 5")
        self.assertEqual(rc, 0)
        self.assertEqual(pre["updatedInput"]["description"], "d")      # other fields preserved
        self.assertNotIn("permissionDecision", pre)                     # never auto-approves
        self.assertIn("argshape:off", pre["additionalContext"])
        st = subprocess.run(self.cli_cmd("fixes"), capture_output=True, text=True, env=self.env).stdout
        self.assertIn("1/1/0", st)

    def test_optout_demotes(self):
        self.execute("fakecli list --limt 5")
        self.execute("fakecli list --limt 5")
        pre = self.hook("pre", "fakecli list --limt 5 # argshape:off")
        self.assertIn("demoted", pre["additionalContext"])
        pre, ran, rc, _ = self.execute("fakecli list --limt 5")
        self.assertEqual(ran, "fakecli list --limt 5")                  # advice only now
        self.assertIn("rejected", pre["additionalContext"])

    def test_session_pair(self):
        _, _, rc, _ = self.execute("fakecli list -n 3")
        self.assertNotEqual(rc, 0)
        self.execute("fakecli list --limit 3")
        pre, ran, _, _ = self.execute("fakecli list -n 3", session="s2")
        self.assertEqual(ran, "fakecli list --limit 3")                 # corroborated by --help

    def test_binary_change_invalidates(self):
        self.execute("fakecli list --limt 5")
        os.utime(self.cli, (time.time() + 5, time.time() + 5))
        pre, ran, _, _ = self.execute("fakecli list --limt 5")
        self.assertEqual(ran, "fakecli list --limt 5")

    def test_mode_tiers(self):
        self.execute("fakecli list --limt 5")
        subprocess.run(self.cli_cmd("mode", "advise"), env=self.env, check=True, capture_output=True)
        pre = self.hook("pre", "fakecli list --limt 5")
        self.assertNotIn("updatedInput", pre)
        self.assertIn("--limit", pre["additionalContext"])
        subprocess.run(self.cli_cmd("mode", "correct"), env=self.env, check=True, capture_output=True)
        subprocess.run(self.cli_cmd("mode", "advise", "--bin", "fakecli"), env=self.env, check=True, capture_output=True)
        self.assertNotIn("updatedInput", self.hook("pre", "fakecli list --limt 5"))
        subprocess.run(self.cli_cmd("mode", "off"), env=self.env, check=True, capture_output=True)
        self.assertIsNone(self.hook("pre", "fakecli list --limt 5"))

    def test_breaker(self):
        self.execute("fakecli list --limt 5")
        sys.path.insert(0, HERE)
        os.environ["ARGSHAPE_STATE_DIR"] = self.env["ARGSHAPE_STATE_DIR"]
        import sqlite3
        db = sqlite3.connect(os.path.join(self.env["ARGSHAPE_STATE_DIR"], "argshape.db"))
        for i in range(4):
            db.execute("INSERT INTO applied VALUES (?,?,?,?,?,?,?)",
                       (f"b{i}", "s", time.time(), "[]", "a", "b", "fail-related"))
        db.commit()
        db.close()
        self.execute("fakecli list --limt 5")      # settles nothing new but checks breaker on next apply
        st = subprocess.run(self.cli_cmd("status"), capture_output=True, text=True, env=self.env).stdout
        self.assertIn("OPEN", st)
        pre, ran, _, _ = self.execute("fakecli list --limt 5")
        self.assertEqual(ran, "fakecli list --limt 5")

    def test_unrelated_commands_untouched(self):
        self.execute("fakecli list --limt 5")
        for cmd in ["echo --limt", "fakecli show --limt", "grep -rn -- --limt .", "fakecli list '--limt' 5"]:
            pre = self.hook("pre", cmd)
            self.assertFalse(pre and "updatedInput" in pre, cmd)

    def test_numeric_obsolete_option_generalizes(self):
        for n in ("a", "b"):
            with open(os.path.join(self.tmp, n), "w") as f:
                f.write("1\n2\n3\n")
        _, _, rc, post = self.execute("tail -2 a b")
        self.assertNotEqual(rc, 0)
        self.assertIn("-n 2", post["additionalContext"])
        pre, ran, rc, _ = self.execute("tail -3 a b")              # learned for -N, not just -2
        self.assertEqual(ran, "tail -n 3 a b")
        self.assertEqual(rc, 0)
        fixes = subprocess.run(self.cli_cmd("fixes"), capture_output=True, text=True, env=self.env).stdout
        self.assertIn("1/1/0", fixes)                               # outcome stats hit the template row
        pre = self.hook("pre", "head -3 a")                         # other binaries untouched
        self.assertIsNone(pre)
        for valid in ("tail -3 a", "cat a | tail -3"):               # valid contexts untouched
            self.assertIsNone(self.hook("pre", valid), valid)

    def test_dash_value_advice(self):
        _, _, rc, post = self.execute("grep -E '->|x' /dev/null")
        if rc != 0 and post:
            self.assertIn("-e", post["additionalContext"])

    @unittest.skipUnless(shutil.which("sqlite3"), "sqlite3 not installed")
    def test_sql_column_and_mac_shape(self):
        db = os.path.join(self.tmp, "net.db")
        rc, _, err = self.run_cmd(
            f"sqlite3 {db} \"create table devices(mac text, name text); insert into devices values "
            f"('aa:bb:cc:dd:ee:01','a'),('aa:bb:cc:dd:ee:02','b'),('0a:1b:2c:3d:4e:5f','c');\"")
        self.assertEqual(rc, 0, err)
        _, _, rc, post = self.execute(f"sqlite3 -header {db} \"select nme from devices\"")
        self.assertNotEqual(rc, 0)
        self.assertIn("name", post["additionalContext"])           # catalog-learned
        pre, ran, rc, _ = self.execute(f"sqlite3 -header {db} \"select nme from devices\"")
        self.assertEqual(ran, f"sqlite3 -header {db} \"select name from devices\"")
        self.assertEqual(rc, 0)
        self.execute(f"sqlite3 -header {db} \"select * from devices\"")   # learns mac shape
        pre, ran, rc, post = self.execute(
            f"sqlite3 -header {db} \"select name from devices where mac = 'AA-BB-CC-DD-EE-02'\"")
        self.assertIn("'aa:bb:cc:dd:ee:02'", ran)
        self.assertEqual(rc, 0)
        out = os.path.join(self.tmp, "results.csv")
        with open(out, "w") as f:
            f.write("agent data\n")
        self.run_cmd(f"sqlite3 {db} \"create table hosts(hname text)\"")
        _, _, rc, _ = self.execute(f"sqlite3 -header {db} \"select hnme from hosts\" > {out}")
        self.assertNotEqual(rc, 0)
        with open(out) as f:
            self.assertNotIn("ARGSHAPE", f.read())                 # catalog rerun never replays a redirect
        shapes = subprocess.run(self.cli_cmd("shapes"), capture_output=True, text=True, env=self.env).stdout
        self.assertIn("colon/lower", shapes)
        self.assertNotIn("ee:01", shapes)                          # shapes only, never values


if __name__ == "__main__":
    unittest.main()
