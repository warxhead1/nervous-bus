"""argshape tests: parser units plus end-to-end learn -> correct loops.

End-to-end cases drive the real hook entry point (`argshape.py hook ...`) as a
subprocess with JSON on stdin, against a throwaway state dir, a real fake CLI
(argparse, so its errors and --help are genuine), and a real sqlite database.
"""
import json
import os
import shutil
import sqlite3
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

# A getopt-style tool with grep's option letters. Installed as `ugrep` it echoes the whole
# bad word (ugrep, rg); under any other name it reports the first bad character (GNU).
# `-%` is a documented symbol option (ugrep's --bool) and `-@` one its --help does not list.
FAKE_GETOPT = r'''#!/usr/bin/env python3
import os, sys
NAME = os.path.basename(sys.argv[0])
FLAGS, TAKES = set("Ein%@"), set("eAd")
LONG = {"bool": 0, "extended-regexp": 0, "ignore-case": 0, "regexp": 1, "after-context": 1, "delimiter": 1, "include": 1}
HELP = """Usage: NAME [OPTION]... [PATTERN] [FILE]...
  -E, --extended-regexp     patterns are extended
  -i, --ignore-case         ignore case
  -n, --line-number         number lines
  -%, --bool                boolean query
  -e, --regexp=PATTERN      use PATTERN
  -A, --after-context=NUM   lines after
  -d, --delimiter=CHAR      field delimiter
      --include=GLOB        only matching files
""".replace("NAME", NAME)


def bad(word, ch=None, long=False):
    if NAME.endswith("ugrep"):
        sys.stderr.write("ugrep: invalid option %s\nFor more help, run `ugrep --help' or `ugrep --help %s'\n" % (word, word))
    elif long:
        sys.stderr.write("%s: unrecognized option '%s'\nTry '%s --help' for more information.\n" % (NAME, word, NAME))
    else:
        sys.stderr.write("%s: invalid option -- '%s'\nUsage: %s [OPTION]... [PATTERN] [FILE]...\n" % (NAME, ch, NAME))
    sys.exit(2)


args, pats, files, i, done = sys.argv[1:], [], [], 0, False
if args == ["--help"]:
    print(HELP)
    sys.exit(0)
while i < len(args):
    a = args[i]
    i += 1
    if done or a == "-" or not a.startswith("-"):
        files.append(a)
    elif a == "--":
        done = True
    elif a.startswith("--"):
        name, eq, val = a[2:].partition("=")
        hit = [k for k in LONG if k == name] or [k for k in LONG if k.startswith(name)]
        if len(hit) != 1:
            bad(a, long=True)
        if LONG[hit[0]] and not eq:
            val, i = args[i], i + 1
        if hit[0] == "regexp":
            pats.append(val)
    else:
        j = 1
        while j < len(a):
            c = a[j]
            if c in FLAGS:
                j += 1
            elif c in TAKES:
                val = a[j + 1:] or args[i]
                i += 0 if a[j + 1:] else 1
                if c == "e":
                    pats.append(val)
                break
            else:
                bad(a, ch=c)
print("ok", pats, files)
'''

# gawk-style --help: the value is a lowercase word (`-F fs`), which a help parser cannot tell from prose.
FAKE_AWKISH = r'''#!/usr/bin/env python3
import sys
HELP = """Usage: awkish [options] 'program' file ...
POSIX options:\t\tGNU long options: (standard)
\t-f progfile\t\t--file=progfile
\t-F fs\t\t\t--field-separator=fs
\t-v var=val\t\t--assign=var=val
\t-q\t\t\t--quiet
"""
args = sys.argv[1:]
if args == ["--help"]:
    print(HELP)
    sys.exit(0)
i = 0
while i < len(args) and args[i].startswith("-") and args[i] not in ("-", "--"):
    a, i = args[i], i + 1
    if a[1] in "fFv":
        i += 1 if len(a) == 2 else 0
    elif a[1] != "q":
        sys.stderr.write("awkish: invalid option -- '%s'\n" % a[1])
        sys.exit(2)
print("ok")
'''


# A getopt-style tool whose --help lists no options: it rejects a dash value, and argshape has no spec to read.
FAKE_BLIND = r'''#!/usr/bin/env python3
import sys
if sys.argv[1:] == ["--help"]:
    print("blind: no option summary available")
    sys.exit(0)
for a in sys.argv[1:]:
    if a.startswith("-") and a != "-":
        sys.stderr.write("blind: invalid option -- '%s'\\n" % a[1])
        sys.exit(2)
print("ok")
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
        "Exit code 2\nugrep: invalid option ->|rejected|error\nFor more help, run `ugrep --help' or `ugrep --help ->|rejected|error'":
            ("dash_value", "->|rejected|error", None),
        "ugrep: invalid option -> foo\nFor more help, run `ugrep --help' or `ugrep --help -> foo'": ("dash_value", "-> foo", None),
        "rg: unrecognized flag --- FAIL|no such file|Error:|^ok\n": ("dash_value", "--- FAIL|no such file|Error:|^ok", None),
        "rg: unrecognized flag ->\n": ("dash_value", "->", None),
        "grep: invalid option -- '>'\nUsage: grep [OPTION]... PATTERNS [FILE]...": ("dash_value", "->", None),
        "grep: invalid option -- ' '\nUsage: grep [OPTION]... PATTERNS [FILE]...": ("dash_value", "- ", None),
        "illegal option -- >\nusage: grep [-abc] pattern": ("dash_value", "->", None),
        "rg: unrecognized flag --colr\n": ("flag", "--colr", None),
        "ugrep: invalid option --colr\n": ("flag", "--colr", None),
        "cat: invalid option -- 'V'\nTry 'cat --help'": ("flag", "-V", None),
    }

    def test_cases(self):
        for text, (kind, tok, hint) in self.CASES.items():
            r = errclass.classify(text)
            self.assertTrue(r, text)
            self.assertEqual((r[0].kind, r[0].token, r[0].hint), (kind, tok, hint), text)

    def test_dash_value_names_the_program(self):
        r = errclass.classify("ugrep: invalid option ->|x\nFor more help, run `ugrep --help'")
        self.assertEqual((r[0].kind, r[0].prog), ("dash_value", "ugrep"))

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

    def test_short_alias_shares_the_long_options_arity(self):
        gnu = """Usage: grep [OPTION]... PATTERNS [FILE]...
  -E, --extended-regexp     PATTERNS are extended regular expressions
  -e, --regexp=PATTERNS     use PATTERNS for matching
  -A, --after-context=NUM   print NUM lines of trailing context
  -s, --no-messages         suppress error messages
      --color[=WHEN]        use markers
"""
        sp = helpspec.parse_help(gnu)
        self.assertTrue(sp.opts["-e"] and sp.opts["-A"])
        self.assertFalse(sp.opts["-E"] or sp.opts["-s"] or sp.opts["--color"])

    def test_symbol_short_options_are_listed(self):
        ug = """Usage: ugrep [OPTIONS] [PATTERN] [-e PATTERN] [FILE ...]
    --all, -@
            Search all files except those explicitly excluded.
    --bool, -%, -%%
            Specifies a Boolean query.
    --heading, -+
            Group matches per file.
    --tree, -^
            Output directories with matching files in a tree.
    -n, --line-number
            Number lines.
    -#, --shift=[N]
            less-style
"""
        sp = helpspec.parse_help(ug)
        self.assertEqual({o for o in sp.opts if not o.startswith("--")}, {"-@", "-%", "-%%", "-+", "-^", "-#", "-n"})
        self.assertIn("--bool", sp.opts)

    def test_a_number_sign_is_not_a_symbol_option(self):
        sp = helpspec.parse_help("  -n, --lines=[-+]NUM     output NUM lines; with +, skip them\n  -q, --quiet     no output\n")
        self.assertEqual({o for o in sp.opts if not o.startswith("--")}, {"-n", "-q"})

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


class BacktestTests(unittest.TestCase):
    """backtest.py scores the dash-value rule against what each replayed call really did."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="argshape-bt-")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        with open(os.path.join(self.bin, "ugrep"), "w") as f:
            f.write(FAKE_GETOPT.replace("#!/usr/bin/env python3", "#!" + sys.executable))
        os.chmod(os.path.join(self.bin, "ugrep"), 0o755)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    BAD = "Exit code 2\nugrep: invalid option ->|{}\nFor more help, run `ugrep --help' or `ugrep --help ->|{}'"

    def write_session(self, root, calls):
        proj = os.path.join(root, "projects", "p1")
        os.makedirs(proj, exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        with open(os.path.join(proj, "s1.jsonl"), "w") as f:
            for i, (cmd, output, is_error) in enumerate(calls):
                base = {"sessionId": "s1", "timestamp": stamp, "cwd": self.tmp}
                f.write(json.dumps({**base, "message": {"content": [
                    {"type": "tool_use", "id": f"t{i}", "name": "Bash", "input": {"command": cmd}}]}}) + "\n")
                f.write(json.dumps({**base, "message": {"content": [
                    {"type": "tool_result", "tool_use_id": f"t{i}", "content": output, "is_error": is_error}]}}) + "\n")

    def run_backtest(self, *extra, roots=()):
        env = {**os.environ, "CLAUDE_CONFIG_DIR": os.path.join(self.tmp, "claude"),
               "PATH": self.bin + ":" + os.environ["PATH"],
               "NBUS_TRANSCRIPTS_CLAUDE": os.pathsep.join(os.path.join(r, "projects") for r in roots)}
        p = subprocess.run([sys.executable, "-I", os.path.join(HERE, "backtest.py"), "--days", "1", "--agent", "claude",
                            "--state", os.path.join(self.tmp, "state"), *extra],
                           capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def test_scores_prevented_false_and_counts_a_twice_seen_call_once(self):
        calls = [("printf x | ugrep -E '->|zzz'", self.BAD.format("zzz", "zzz"), True),     # teaches the rule
                 ("printf x | ugrep -E '->|qqq'", self.BAD.format("qqq", "qqq"), True),     # rewritten: prevented
                 ("printf x | ugrep -e '->x'", "ok ['->x'] []", False),                     # valid: untouched
                 ("printf x | ugrep -E '->|www'", "ok [] ['->|www']", False)]               # claims success: a false rewrite
        self.write_session(os.path.join(self.tmp, "claude"), calls)
        mirror = os.path.join(self.tmp, "mirror")
        self.write_session(mirror, calls)
        out = self.run_backtest(roots=[mirror])
        self.assertIn("calls=4 dupes_skipped=4", out)
        self.assertIn("dash_value: failures=2 (masked_by_pipe=0) rewrites=2 prevented=1 false=1 unclear=0", out)
        rewrites = json.load(open(os.path.join(self.tmp, "state", "out.json")))
        self.assertEqual([r["dash_rule"] for r in rewrites if "dash_rule" in r], ["prevented", "false"])
        self.assertEqual(rewrites[0]["after"], "printf x | ugrep -E -e '->|qqq'")

    def test_seeding_opens_the_rule_for_a_binary_that_never_failed(self):
        self.write_session(os.path.join(self.tmp, "claude"), [
            ("printf x | ugrep -E '->|zzz'", "ok [] ['->|zzz']", False), ("printf x | ugrep -e '->x'", "ok ['->x'] []", False)])
        self.assertIn("dash_value: failures=0 (masked_by_pipe=0) rewrites=0", self.run_backtest())
        out = self.run_backtest("--seed-dash-value", "ugrep")
        self.assertIn("rewrites=1 prevented=0 false=1", out)

    def test_output_that_quotes_a_rejection_is_not_counted_as_one(self):
        self.write_session(os.path.join(self.tmp, "claude"), [
            ("cat notes.txt", self.BAD.format("zzz", "zzz").split("\n", 1)[1], False)])
        self.assertIn("dash_value: failures=0", self.run_backtest())

    def test_a_rejection_hidden_by_a_pipe_still_counts_as_prevented(self):
        self.write_session(os.path.join(self.tmp, "claude"), [
            ("printf x | ugrep -E '->|zzz' | head", self.BAD.format("zzz", "zzz").split("\n", 1)[1], False)])
        out = self.run_backtest("--seed-dash-value", "ugrep")
        self.assertIn("failures=1 (masked_by_pipe=1) rewrites=1 prevented=1 false=0", out)


class HookHarness(unittest.TestCase):
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



class EndToEnd(HookHarness):
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

    def test_harness_failures_are_not_cli_failures(self):
        deny = ("This agent is isolated in the worktree /w, but this command is too complex to "
                "verify that it stays inside the worktree. Refusing to run it.")
        for text in (deny, "Exit code 1\nCommand output was lost: the temp filesystem is full"):
            self.assertIsNone(self.hook("fail", "fakecli list --limt 5 | tail -3", error=text))
        db = sqlite3.connect(os.path.join(self.env["ARGSHAPE_STATE_DIR"], "argshape.db"))
        self.assertEqual(db.execute("SELECT count(*) FROM rejects").fetchone()[0], 0)
        self.assertEqual(db.execute("SELECT count(*) FROM specs").fetchone()[0], 0)
        self.assertEqual(db.execute("SELECT DISTINCT name, err_class FROM sigs").fetchall(), [("(harness)", "harness")])
        sigs = subprocess.run(self.cli_cmd("sigs"), capture_output=True, text=True, env=self.env).stdout
        self.assertNotIn("isolated", sigs)
        status = subprocess.run(self.cli_cmd("status"), capture_output=True, text=True, env=self.env).stdout
        self.assertIn("+2 harness", status)

    def install(self, name, text=FAKE_GETOPT):
        path = os.path.join(self.bin, name)
        with open(path, "w") as f:
            f.write(text.replace("#!/usr/bin/env python3", "#!" + sys.executable))
        os.chmod(path, 0o755)
        return path

    def untouched(self, *commands):
        for cmd in commands:
            pre = self.hook("pre", cmd)
            self.assertFalse(pre and "updatedInput" in pre, cmd)

    def install_grep_over_ugrep(self):
        """Claude Code's `grep` is ugrep behind a shell function."""
        self.install("ugrep")
        with open(os.path.join(self.bin, "grep"), "w") as f:
            f.write('#!/bin/sh\nexec ugrep "$@"\n')
        os.chmod(os.path.join(self.bin, "grep"), 0o755)

    def statedb(self):
        return sqlite3.connect(os.path.join(self.env["ARGSHAPE_STATE_DIR"], "argshape.db"), isolation_level=None)

    def test_dash_value_is_corrected_with_dash_e(self):
        self.install_grep_over_ugrep()
        self.untouched("printf 'a->b\\n' | grep -E '->|zzz'")                   # no history yet
        _, _, rc, post = self.execute("printf 'a->b\\n' | grep -E '->|zzz'")
        self.assertNotEqual(rc, 0)
        self.assertIn("-e", post["additionalContext"])
        pre, ran, rc, _ = self.execute("printf 'a->b\\n' | grep -E '->|qqq'")
        self.assertEqual(ran, "printf 'a->b\\n' | grep -E -e '->|qqq'")
        self.assertEqual(rc, 0)
        self.assertNotIn("permissionDecision", pre)
        self.assertIn("argshape:off", pre["additionalContext"])
        for cmd, want in [
                ('printf x | ugrep "->x"', 'printf x | ugrep -e "->x"'),
                ("printf x | ugrep -n '-> a b' file1", "printf x | ugrep -n -e '-> a b' file1"),
                ("printf x | ugrep '--- FAIL|x' f", "printf x | ugrep -e '--- FAIL|x' f"),
                ("printf x | ugrep -e foo '->x'", "printf x | ugrep -e foo -- '->x'"),   # a file, not a second pattern
                ("printf x | ugrep -ne foo '->x'", "printf x | ugrep -ne foo -- '->x'"),   # -e at the end of a cluster
        ]:
            pre = self.hook("pre", cmd)
            self.assertEqual(pre and pre.get("updatedInput", {}).get("command"), want, cmd)
        self.untouched("printf x | ugrep -v'|x' f")            # -v is not in the learned --help: unknown, leave it

    def test_dash_value_leaves_valid_commands_alone(self):
        self.install("ugrep")
        self.execute("printf x | ugrep -E '->|zzz'")
        self.untouched(
            "printf x | ugrep -i zzz",
            "printf x | ugrep -e '->x'",                 # the value of -e
            "printf x | ugrep --regexp '->x'",
            "printf x | ugrep --regexp='->x'",
            "printf x | ugrep -e'->x'",                  # value attached
            "printf x | ugrep -ie '->x'",                # value of the -e at the end of a cluster
            "printf x | ugrep -A '-> x'",
            "printf x | ugrep --include='-> y' zzz",     # a real option with a value
            "printf x | ugrep -E -- '->x'",              # already past the options
            "printf x | ugrep -d' ' zzz",
            "printf x | ugrep '->x' -e foo",             # a file operand with an option after it
            "printf x | ugrep '-> a' '-> b'",            # two candidates: ambiguous
            "printf x | ugrep -E $PAT '->x'",            # $PAT may expand to an option that takes it
            "printf x | ugrep --limt '->x'",             # an unknown option: someone else's problem
        )

    def test_dash_value_other_getopt_tool_gets_double_dash(self):
        self.install("lister")
        _, _, rc, post = self.execute("lister '-> x'")
        self.assertNotEqual(rc, 0)
        self.assertIn("--", post["additionalContext"])
        pre, ran, rc, _ = self.execute("lister -i '-> y'")
        self.assertEqual(ran, "lister -i -- '-> y'")
        self.assertEqual(rc, 0)
        pre = self.hook("pre", "lister -i '-?x'")          # `?` was always parsed: a spec without `-?` has no `-?`
        self.assertEqual(pre["updatedInput"]["command"], "lister -i -- '-?x'")
        self.untouched(
            "lister b '-> y'",                 # not the first operand: some parsers would print the `--`
            "lister '-> y' -i",                # an option follows: `--` would turn it into an operand
            "lister '-> y' $MORE",             # so might this
            "lister -- '-> y'",                # already past the options
            "lister -d '-> y'",                # the value of -d
            "lister -d'-> y'",
            "lister --include='-> y' f",
            "lister --inc '-> y'",             # unique abbreviation of a value-taking option
            "lister -i f g",
            "lister '-> y' '-> z'",            # two candidates: ambiguous
        )

    def test_dash_value_is_learned_from_a_rejection_a_pipe_hid(self):
        self.install("ugrep")
        _, _, rc, post = self.execute("printf x | ugrep -E '->|zzz' | head -1")
        self.assertEqual(rc, 0)                       # head ended the call at exit 0: the success hook sees it
        self.assertIn("-e", post["additionalContext"])
        _, ran, _, _ = self.execute("printf x | ugrep -E '->|qqq' | head -1")
        self.assertEqual(ran, "printf x | ugrep -E -e '->|qqq' | head -1")

    def test_a_pipe_hidden_rejection_after_a_huge_output_is_still_seen(self):
        self.install("ugrep")
        cmd = "printf x | ugrep -E '->|zzz' | head -1"
        pre = self.hook("pre", cmd, "s1", "big1")
        self.assertIsNone(pre)
        out = "line of output\n" * 400000        # 6 MB before the rejection, on stdout
        post = self.hook("post", cmd, "s1", "big1", tool_response={
            "stdout": out, "stderr": "ugrep: invalid option ->|zzz\n", "interrupted": False})
        self.assertIn("-e", post["additionalContext"])

    def test_dash_value_is_not_learned_from_output_that_only_quotes_a_rejection(self):
        self.install("ugrep")
        self.execute("printf 'ugrep: invalid option ->|zzz\\n' | cat")       # no ugrep call in this command
        self.execute("printf 'ugrep: invalid option ->|zzz\\n' | ugrep -E y | cat")    # and no such word in this one
        self.untouched("printf x | ugrep -E '->|qqq'")

    def test_dash_value_leaves_an_option_alone_when_help_hides_its_value(self):
        self.install("awkish", FAKE_AWKISH)
        self.execute("awkish '-> x' f")
        pre, ran, rc, _ = self.execute("awkish '-> y'")
        self.assertEqual(ran, "awkish -- '-> y'")
        self.assertEqual(rc, 0)
        self.untouched(
            "awkish -F: '{print $1}' f",
            "awkish -F'\\t' '{print $1}' f",
            "awkish -F '|' '{print $1}' f",
            "awkish -v x=a:b -F: '{print $1}'",
            "awkish -q -F:, 'x'",
        )

    def test_dash_value_needs_this_binary_to_have_rejected_one(self):
        self.install("ugrep")
        self.install("lister")
        self.install("other")
        self.execute("printf x | ugrep -E '->|zzz'")
        self.untouched("other '-> y'", "lister '-> y'", "printf x | other -E '->|zzz'")
        self.execute("lister a '-> x'")
        os.utime(os.path.join(self.bin, "lister"), (time.time() + 5, time.time() + 5))     # a different build
        self.untouched("lister a '-> y'")
        self.untouched("printf x | ugrep -e '->x'", "printf x | ugrep -i x")

    def test_dash_value_leaves_a_symbol_option_alone(self):
        self.install("ugrep")
        self.execute("printf x | ugrep -E '->|zzz'")
        for cmd in ("ugrep -% 'a b' f", "ugrep -%% 'a b' f"):         # `-%` is in the learned --help
            self.assertEqual(self.run_cmd(cmd)[2].count("invalid option"), 0, cmd)      # a valid command
            self.untouched(cmd)
        # `-@` is a real option its --help does not mention (or a spec learned before symbol options
        # were parsed): not being listed must not make it an operand.
        self.assertEqual(self.run_cmd("ugrep -@ 'a b' f")[2].count("invalid option"), 0)
        self.untouched("ugrep -@ 'a b' f", "ugrep -@I 'a b' f", "printf x | ugrep -+ 'a b' f")
        pre, ran, rc, _ = self.execute("printf x | ugrep -E '->|qqq'")        # the rule is still open
        self.assertIn("-e", ran)

    def test_a_known_getopt_tool_is_left_alone_until_it_rejects_a_dash_value(self):
        self.install("lister")
        _, _, rc, _ = self.execute("lister --bogus")           # a flag rejection: the spec is learned
        self.assertNotEqual(rc, 0)
        db = self.statedb()
        self.assertEqual(db.execute("SELECT COUNT(*) FROM specs").fetchone()[0], 1)         # it is known ...
        self.assertEqual(db.execute("SELECT COUNT(*) FROM rejects WHERE kind='dash_value'").fetchone()[0], 0)
        self.untouched("lister '-> y'", "lister -i '-> y'")                               # ... and not yet getopt-proven
        self.execute("lister '-> x'")
        pre, ran, rc, _ = self.execute("lister -i '-> y'")
        self.assertEqual(ran, "lister -i -- '-> y'")
        self.assertEqual(rc, 0)

    def test_a_tool_with_no_readable_help_is_left_alone(self):
        self.install("blind", FAKE_BLIND)
        _, _, rc, _ = self.execute("blind '-> x'")             # it rejects a dash value, but --help has nothing to learn
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.statedb().execute("SELECT COUNT(*) FROM specs").fetchone()[0], 0)
        self.untouched("blind '-> y'")
        log = os.path.join(self.env["ARGSHAPE_STATE_DIR"], "errors.log")      # left alone on purpose, not by a swallowed crash
        self.assertFalse(os.path.exists(log) and os.path.getsize(log), log)

    def test_a_tool_with_subcommands_gets_no_double_dash_in_the_verb_slot(self):
        import core
        self.install("lister")
        self.execute("lister '-> x'")
        self.execute("fakecli list --limt 5")
        db = self.statedb()
        bin_key, _ = core.bin_identity(db, self.cli, self.tmp)
        self.assertTrue(core.store.get_spec(db, bin_key, "").subcmds)
        db.execute("INSERT INTO rejects (bin_key, name, ctx, kind, token, n) VALUES (?, 'fakecli', '', 'dash_value', ?, 1)",
                   (bin_key, core.DASH_KEY))
        pre, ran, rc, _ = self.execute("lister '-> y'")
        self.assertEqual(ran, "lister -- '-> y'")              # the same shape, a flat tool: corrected
        self.untouched("fakecli '-> x'")                       # a verb tool: `--` is not "end of options" for its verb

    def test_a_dash_fix_that_is_itself_rejected_switches_the_rule_off(self):
        self.install("lister")
        self.execute("lister '-> x'")
        pre = self.hook("pre", "lister -i '-> y'", "s1", "t-dash")
        ran = pre["updatedInput"]["command"]
        self.assertEqual(ran, "lister -i -- '-> y'")
        # A parser that took the `--` badly. GNU names only the first bad character, so the token
        # (`->`) is neither the word we fixed nor the `--` we added.
        self.hook("fail", ran, "s1", "t-dash", error="Exit code 2\nlister: invalid option -- '>'\nUsage: lister [OPTION]...\n")
        off = self.statedb().execute("SELECT k, v FROM meta WHERE k LIKE 'dash_rule_off:%'").fetchall()
        self.assertEqual(len(off), 1, off)
        pre = self.hook("pre", "lister -i '-> z'")
        self.assertNotIn("updatedInput", pre)
        self.assertIn("advice", pre["additionalContext"])

    def test_dash_value_demote_and_promote_take_either_name_of_the_live_grep(self):
        self.install_grep_over_ugrep()
        self.execute("printf 'a->b\\n' | grep -E '->|zzz'")                  # stored under `grep`, keyed on ugrep's file
        cmd = "printf 'a->b\\n' | grep -E '->|qqq'"
        fixed = "printf 'a->b\\n' | grep -E -e '->|qqq'"
        self.assertEqual(self.execute(cmd)[1], fixed)

        def argshape(*args):
            return subprocess.run(self.cli_cmd(*args), capture_output=True, text=True, env=self.env).stdout

        for who in ("ugrep", "grep"):
            self.assertIn("demoted dash_value rule for 1 build(s)", argshape("demote", who, "dash_value"), who)
            self.assertEqual(self.execute(cmd)[1], cmd, who)                       # advice only now
            self.assertIn("promoted dash_value rule for 1 build(s)", argshape("promote", who, "dash_value"), who)
            self.assertEqual(self.execute(cmd)[1], fixed, who)
        self.assertIn("for 0 build(s)", argshape("demote", "rg", "dash_value"))        # a name that is neither

    def test_dash_value_optout_demotes_the_rule(self):
        self.install("ugrep")
        self.execute("printf x | ugrep -E '->|zzz'")
        pre, ran, _, _ = self.execute("printf x | ugrep -E '->|qqq'")
        self.assertIn("-e", ran)
        pre = self.hook("pre", "printf x | ugrep -E '->|qqq' # argshape:off")
        self.assertIn("demoted", pre["additionalContext"])
        pre, ran, _, _ = self.execute("printf x | ugrep -E '->|qqq'")
        self.assertEqual(ran, "printf x | ugrep -E '->|qqq'")
        self.assertIn("advice", pre["additionalContext"])
        out = subprocess.run(self.cli_cmd("promote", "ugrep", "dash_value"), capture_output=True, text=True, env=self.env).stdout
        self.assertIn("promoted", out)
        pre, ran, _, _ = self.execute("printf x | ugrep -E '->|qqq'")
        self.assertIn("-e", ran)

    def test_dash_value_follows_the_mode_tiers(self):
        self.install("ugrep")
        self.execute("printf x | ugrep -E '->|zzz'")
        subprocess.run(self.cli_cmd("mode", "advise"), env=self.env, check=True, capture_output=True)
        pre = self.hook("pre", "printf x | ugrep -E '->|qqq'")
        self.assertNotIn("updatedInput", pre)
        self.assertIn("-e", pre["additionalContext"])
        subprocess.run(self.cli_cmd("mode", "correct"), env=self.env, check=True, capture_output=True)
        subprocess.run(self.cli_cmd("mode", "advise", "--bin", "ugrep"), env=self.env, check=True, capture_output=True)
        self.assertNotIn("updatedInput", self.hook("pre", "printf x | ugrep -E '->|qqq'"))
        subprocess.run(self.cli_cmd("mode", "off"), env=self.env, check=True, capture_output=True)
        self.assertIsNone(self.hook("pre", "printf x | ugrep -E '->|qqq'"))

    def test_dash_value_never_publishes_an_off_schema_event(self):
        schema = json.load(open(os.path.join(HERE, "..", "..", "schemas", "agent.cli.correction.v1.json")))
        sent = os.path.join(self.tmp, "sent.jsonl")
        nervous = os.path.join(self.bin, "nervous")
        with open(nervous, "w") as f:
            f.write('#!/bin/sh\nprintf "%s\\n" "$3" >> ' + sent + '\n')
        os.chmod(nervous, 0o755)
        self.env.pop("ARGSHAPE_NO_BUS")
        self.install("ugrep")
        self.execute("printf x | ugrep -E '->|zzz'")
        pre, ran, _, _ = self.execute("printf x | ugrep -E '->|qqq'")
        self.assertIn("-e", ran)
        self.hook("pre", "printf x | ugrep -E '->|qqq' # argshape:off")
        time.sleep(0.5)       # the hook publishes fire-and-forget
        events = [json.loads(line) for line in open(sent)] if os.path.exists(sent) else []
        for ev in events:
            if "kind" in ev:
                self.assertIn(ev["kind"], schema["properties"]["kind"]["enum"], ev)
            if "source" in ev:
                self.assertIn(ev["source"], schema["properties"]["source"]["enum"], ev)

    def test_a_crash_in_the_dash_rule_fails_open(self):
        import core
        from unittest import mock
        self.execute("fakecli list --limt 5")
        db = sqlite3.connect(os.path.join(self.env["ARGSHAPE_STATE_DIR"], "argshape.db"), isolation_level=None)
        bin_key, _ = core.bin_identity(db, "fakecli", self.tmp)
        db.execute("INSERT INTO rejects (bin_key, name, ctx, kind, token, n) VALUES (?, 'fakecli', '', 'dash_value', ?, 1)",
                   (bin_key, core.DASH_KEY))
        cfg = {"mode": "correct"}
        err_dir = os.path.join(self.tmp, "errs")
        with mock.patch.object(core.store, "STATE_DIR", err_dir), \
                mock.patch.object(core, "_dash_value_plan", side_effect=RuntimeError("boom")):
            with mock.patch.dict(os.environ, {"PATH": self.env["PATH"]}):
                r = core.plan(db, cfg, "fakecli list --limt 5", self.tmp)
        self.assertEqual(r["command"], "fakecli list --limit 5")         # the other correction still lands
        self.assertIn("boom", open(os.path.join(err_dir, "errors.log")).read())

    def test_a_crash_learning_a_pipe_hidden_rejection_fails_open(self):
        import core
        from unittest import mock
        self.execute("fakecli list --limt 5")
        db = sqlite3.connect(os.path.join(self.env["ARGSHAPE_STATE_DIR"], "argshape.db"), isolation_level=None)
        err_dir = os.path.join(self.tmp, "errs")
        ev = {"tool_input": {"command": "printf x | ugrep -E '->|a' | head"}, "cwd": self.tmp, "session_id": "s9",
              "tool_response": {"stdout": "", "stderr": "ugrep: invalid option ->|a\n"}}
        with mock.patch.object(core.store, "STATE_DIR", err_dir), \
                mock.patch.object(core, "_learn_masked_dash_values", side_effect=RuntimeError("boom")):
            notes = core.on_success(db, {"mode": "correct"}, ev)
        self.assertEqual(notes, [])
        with open(os.path.join(err_dir, "errors.log")) as f:
            self.assertIn("boom", f.read())

    def test_hook_exits_zero_and_silent_when_the_state_is_unreadable(self):
        self.execute("fakecli list --limt 5")
        with open(os.path.join(self.env["ARGSHAPE_STATE_DIR"], "argshape.db"), "wb") as f:
            f.write(os.urandom(4096))
        for kind, extra in (("pre", {}), ("fail", {"error": "Exit code 2\nugrep: invalid option ->|x\n"}),
                            ("post", {"tool_response": {"stdout": "a b\n1 2\n3 4\n5 6\n"}})):
            self.assertIsNone(self.hook(kind, "printf x | grep -E '->|x'", **extra), kind)
        self.assertTrue(os.path.getsize(os.path.join(self.env["ARGSHAPE_STATE_DIR"], "errors.log")) > 0)

    def real_dash_value(self, name, first, second, want):
        state = os.path.join(self.tmp, "state")
        shutil.rmtree(state, ignore_errors=True)
        _, _, rc, post = self.execute(first)
        self.assertNotEqual(rc, 0, name)
        self.assertIn("-e", post["additionalContext"], name)
        pre, ran, rc, _ = self.execute(second)
        self.assertEqual(ran, want, name)
        self.assertEqual(rc, 0, name)

    def test_dash_value_with_the_real_grep_family(self):
        found = 0
        for name in ("ugrep", "rg", "grep"):
            path = shutil.which(name)
            if not path or (name == "grep" and "GNU" not in subprocess.run(
                    [path, "-V"], capture_output=True, text=True).stdout):
                continue
            found += 1
            with self.subTest(name):
                self.real_dash_value(name, f"printf 'a->b\\n' | {name} '->z'", f"printf 'a->b\\n' | {name} '->b'",
                                     f"printf 'a->b\\n' | {name} -e '->b'")
                if name == "ugrep":      # symbol options only ugrep has, which this build's own --help must list
                    with open(os.path.join(self.tmp, "fb.txt"), "w") as f:
                        f.write("foo bar\n")
                    for opt in ("-%", "-%%", "-@", "-+", "-^"):
                        cmd = f"ugrep {opt} 'foo bar' fb.txt"
                        self.assertEqual(self.run_cmd(cmd)[0], 0, cmd)
                        self.untouched(cmd)
        if not found:
            self.skipTest("none of ugrep, rg, GNU grep installed")

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


class PipefailTests(unittest.TestCase):
    """Static rewrite: verify command piped into a reader gets `set -o pipefail;`."""

    def rw(self, cmd):
        import pipefail
        return pipefail.rewrite(cmd)

    def assertRewritten(self, cmd):
        self.assertEqual(self.rw(cmd), "set -o pipefail; " + cmd, cmd)

    def assertLeft(self, cmd):
        self.assertIsNone(self.rw(cmd), cmd)

    def test_each_full_reading_consumer_is_rewritten(self):
        for tail in ("tail -20", "tail -n +3", "tee out.log", "tee -a out.log", "wc -l", "sort -u", "cat",
                     "grep FAIL", "grep -v ok", "grep -E 'a|b'", "grep -c x", "grep -n -i x", "rg FAIL",
                     "rg -n x", "egrep x", "grep -e a -e b", "tail -5 | wc -l", "tail -n 5"):
            self.assertRewritten(f"cargo test 2>&1 | {tail}")

    def test_early_exit_consumers_are_left_alone(self):
        for tail in ("head", "head -5", "grep -q x", "grep -m1 x", "grep -m 1 x", "grep -l x", "grep -qE x",
                     "grep -lv x", "grep --quiet x", "grep --max-count=1 x", "grep --max=1 x",
                     "grep -L x", "rg -q x", "rg -l x", "rg -m1 x", "rg --files-with-matches x",
                     "sed -n 1p", "sed 5q", "awk 'NR==1{exit}'", "sort -c", "less", "jq .", "xargs echo",
                     "tail -5 | head -1", "grep x | head"):
            self.assertLeft(f"cargo test 2>&1 | {tail}")

    def test_consumers_that_ignore_stdin_are_left_alone(self):
        for tail in ("grep x file.txt", "grep -e x file.txt", "grep -r x", "rg x src", "tail file.log",
                     "wc -l file", "cat file", "sort file"):
            self.assertLeft(f"cargo test 2>&1 | {tail}")

    def test_pipefail_or_pipestatus_already_present(self):
        self.assertLeft("set -o pipefail; cargo test | tail")
        self.assertLeft("set -euo pipefail\ncargo test | tail")
        self.assertLeft("cargo test | tail; echo ${PIPESTATUS[0]}")
        self.assertLeft("cargo test | tail; echo ${pipestatus[1]}")

    def test_opt_out_and_masking(self):
        self.assertLeft("cargo test | tail # argshape:off")
        self.assertLeft("cargo test | tail || true")
        self.assertLeft("cargo test | tail || :")
        self.assertLeft("cargo test | tail || exit 0")

    def test_first_segment_must_be_a_verify_command(self):
        self.assertLeft("ls -la | tail")
        self.assertLeft("cat x | tail -1")
        self.assertLeft("cargo install foo | tail")
        self.assertLeft("cargo test")                      # no pipe at all
        self.assertLeft("cargo test > out.log 2>&1")
        for v in ("go test ./... -count=1", "go vet ./...", "npm run build", "npm test", "pnpm run lint",
                  "make -C sub test", "python3 -m pytest -x", "pytest -q", "tsc --noEmit", "uv run pytest",
                  "timeout 600 cargo nextest run", "RUST_LOG=x cargo clippy", "bash scripts/run-tests.sh",
                  "ruff check .", "FOO=1 make check", "cmake --build build", "swift test"):
            self.assertRewritten(f"{v} 2>&1 | tail -5")

    def test_multi_pipe_and_prefixes(self):
        self.assertRewritten("cargo test 2>&1 | grep -v ok | tail -20")
        self.assertRewritten("cargo test 2>&1 | grep -v ok | tee x | wc -l")
        self.assertLeft("cargo test 2>&1 | grep -v ok | head")
        self.assertRewritten("cd sub && cargo test 2>&1 | tail")
        self.assertRewritten("cd sub; cargo test 2>&1 | tail")
        self.assertRewritten("(cd sub && cargo test 2>&1 | tail)")
        self.assertRewritten("cargo build 2>&1 | tail -3 && cargo test 2>&1 | tail -3")
        self.assertRewritten("cargo test 2>&1 | tail -3 && echo ok")
        self.assertRewritten("cargo test 2>&1 | tail -3 || echo FAILED")
        self.assertRewritten("cargo test |& tail")

    def test_follow_up_after_semicolon_is_left_alone(self):
        self.assertLeft("cargo test | tail; echo $?")
        self.assertLeft("cargo test | tail\necho done")
        self.assertLeft("cargo test | tail &")

    def test_line_wide_pipefail_must_be_safe_for_every_pipe(self):
        self.assertLeft("ls | head -3; cargo test | tail")
        self.assertLeft("ls | head -3 && cargo test | tail")
        self.assertLeft("ls | wc -l && cargo test | tail")           # non-verify pipe in the final chain
        self.assertRewritten("ls | wc -l; cargo test | tail")        # earlier list, safe consumer
        self.assertLeft("cargo test | tail && ls | head")

    def test_unplaceable_shapes_are_left_alone(self):
        self.assertLeft("(cargo test) | tail")
        self.assertLeft("{ cargo test; } | tail")
        self.assertLeft("for x in a; do cargo test; done | tail")
        self.assertLeft("cargo test | tee >(cat)")
        self.assertLeft("cargo test $(ls | head -1) | tail")
        self.assertLeft("cargo test | tail | ")
        self.assertLeft("")

    def test_pipeline_in_loop_body(self):
        self.assertRewritten("for d in a b; do (cd $d && cargo test 2>&1 | tail -2); done")
        self.assertLeft("if cargo test 2>&1 | tail -2; then echo ok; fi")      # status is the branch's, not the verify's

    SHAPES = ("{v} 2>&1 | tail -3", "{v} 2>&1 | tee {t}/o.log | tail -3", "{v} 2>&1 | wc -l",
              "{v} 2>&1 | grep -v zzz | sort", "{v} 2>&1 | cat")

    def shells(self):
        return [s for s in ("/bin/bash", "/usr/bin/zsh") if os.path.exists(s)]

    def run_shell(self, shell, cmd, tmp):
        return subprocess.run([shell, "-c", cmd], capture_output=True, text=True, cwd=tmp)

    def fake(self, tmp, name, body):
        path = os.path.join(tmp, name)
        with open(path, "w") as f:
            f.write("#!/bin/sh\n" + body + "\n")
        os.chmod(path, 0o755)

    def test_real_shells_exit_status_follows_the_verify(self):
        tmp = tempfile.mkdtemp(prefix="argshape-pf-")
        try:
            self.fake(tmp, "cargo", "echo out; exit 1")
            self.fake(tmp, "pytest", "echo out; exit 0")
            for shell in self.shells():
                for shape in self.SHAPES:
                    raw_f = f"PATH={tmp}:$PATH; " + shape.format(v="cargo test", t=tmp)
                    raw_p = f"PATH={tmp}:$PATH; " + shape.format(v="pytest -q", t=tmp)
                    fixed_f, fixed_p = self.rw(raw_f), self.rw(raw_p)
                    self.assertTrue(fixed_f and fixed_p, shape)
                    self.assertEqual(self.run_shell(shell, raw_f, tmp).returncode, 0, (shell, shape, "masked before"))
                    self.assertNotEqual(self.run_shell(shell, fixed_f, tmp).returncode, 0, (shell, shape))
                    self.assertEqual(self.run_shell(shell, fixed_p, tmp).returncode, 0, (shell, shape, "green stays green"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_real_shells_and_or_follow_ups_print_the_true_verdict(self):
        tmp = tempfile.mkdtemp(prefix="argshape-pf-")
        try:
            self.fake(tmp, "cargo", "echo out; exit 1")
            self.fake(tmp, "pytest", "echo out; exit 0")
            for shell in self.shells():
                ok = f"PATH={tmp}:$PATH; pytest -q 2>&1 | tail -3 && echo GREEN || echo RED"
                bad = f"PATH={tmp}:$PATH; cargo test 2>&1 | tail -3 && echo GREEN || echo RED"
                self.assertIn("GREEN", self.run_shell(shell, bad, tmp).stdout)       # the lie
                self.assertIn("RED", self.run_shell(shell, self.rw(bad), tmp).stdout)
                self.assertIn("GREEN", self.run_shell(shell, self.rw(ok), tmp).stdout)
                self.assertNotIn("RED", self.run_shell(shell, self.rw(ok), tmp).stdout)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_big_output_with_a_full_reading_consumer_stays_green(self):
        tmp = tempfile.mkdtemp(prefix="argshape-pf-")
        try:
            self.fake(tmp, "pytest", "yes line | head -200000\nexit 0")
            for shell in self.shells():
                for consumer in ("tail -1", "wc -l", "grep -c line", "sort -u", "tee /dev/null | tail -1"):
                    cmd = self.rw(f"PATH={tmp}:$PATH; pytest -q | {consumer}")
                    self.assertTrue(cmd)
                    self.assertEqual(self.run_shell(shell, cmd, tmp).returncode, 0, (shell, consumer))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class PipefailHookTests(HookHarness):
    def test_hook_rewrites_without_a_permission_decision_and_without_state(self):
        out = self.hook("pre", "cargo test 2>&1 | tail -5")
        self.assertEqual(out["updatedInput"]["command"], "set -o pipefail; cargo test 2>&1 | tail -5")
        self.assertEqual(out["updatedInput"]["description"], "d")
        self.assertNotIn("permissionDecision", out)

    def test_hook_leaves_alone_head_optout_and_non_verify(self):
        self.assertIsNone(self.hook("pre", "cargo test 2>&1 | head -5"))
        self.assertIsNone(self.hook("pre", "cargo test 2>&1 | tail -5 # argshape:off"))
        self.assertIsNone(self.hook("pre", "ls | tail -5"))

    def test_hook_honours_modes_and_kill_switch(self):
        cmd = "cargo test 2>&1 | tail -5"
        self.env["ARGSHAPE_MODE"] = "off"
        self.assertIsNone(self.hook("pre", cmd))
        self.env.pop("ARGSHAPE_MODE")
        for mode in ("observe", "advise"):
            with open(self.env["ARGSHAPE_CONFIG"], "w") as f:
                json.dump({"mode": mode}, f)
            self.assertIsNone(self.hook("pre", cmd), mode)
        with open(self.env["ARGSHAPE_CONFIG"], "w") as f:
            json.dump({"mode": "correct", "bins": {"pipefail": "off"}}, f)
        self.assertIsNone(self.hook("pre", cmd))

    def test_failing_verify_exits_nonzero_through_the_hook(self):
        for name, body in (("cargo", "echo boom; exit 101"), ("pytest", "echo fine; exit 0")):
            path = os.path.join(self.bin, name)
            with open(path, "w") as f:
                f.write("#!/bin/sh\n" + body + "\n")
            os.chmod(path, 0o755)
        pre = self.hook("pre", "cargo test 2>&1 | tail -5")
        self.assertEqual(self.run_cmd(pre["updatedInput"]["command"])[0], 101)
        pre = self.hook("pre", "pytest -q 2>&1 | tail -5")
        rc, out, _ = self.run_cmd(pre["updatedInput"]["command"])
        self.assertEqual((rc, out.strip()), (0, "fine"))


if __name__ == "__main__":
    unittest.main()
