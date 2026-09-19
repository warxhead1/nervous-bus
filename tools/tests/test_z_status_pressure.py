import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import uuid


class PressurePublisher(unittest.TestCase):
    def fixture(self, fail_first):
        source = (Path(__file__).parents[1] / 'z-status-daemon').read_text()
        start = source.index('    if [[ "$severity" != "$PRESSURE_STATE" ]]; then')
        block = source[start:source.index('\n}\n', start)]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stub = root / 'redis-cli'
            stub.write_text('''#!/usr/bin/env python3
import json, os, sys
with open(os.environ['PRESSURE_FIXTURE_LOG'], 'a') as f:
    f.write(json.dumps(sys.argv[1:]) + '\\n')
sys.exit(1 if os.environ.get('PRESSURE_FIXTURE_FAIL') == '1' else 0)
''')
            stub.chmod(0o755)
            script = '''set -uo pipefail
PRESSURE_STATE=ok
severity=critical
mem_avail_gb=3.5
load_1m=16.2
disk_free_gb=18
swap_used_gb=2
build_count=18
triggers='"ram_low","builds_overloaded"'
log() { :; }
publish() {
''' + block + '\n}\n'
            if fail_first:
                script += 'export PRESSURE_FIXTURE_FAIL=1\npublish\nprintf "%s\\n" "$PRESSURE_STATE"\nunset PRESSURE_FIXTURE_FAIL\n'
            script += 'publish\npublish\nprintf "%s\\n" "$PRESSURE_STATE"\n'
            env = {**os.environ, 'PATH': str(root) + os.pathsep + os.environ['PATH'],
                   'PRESSURE_FIXTURE_LOG': str(root / 'calls.jsonl')}
            result = subprocess.run(['bash', '-c', script], env=env, text=True,
                                    capture_output=True, timeout=10, check=True)
            return result.stdout.splitlines(), [json.loads(line) for line in (root / 'calls.jsonl').read_text().splitlines()]

    def test_canonical_envelope_is_published_once_per_transition(self):
        states, calls = self.fixture(False)
        self.assertEqual(states, ['critical'])
        self.assertEqual(len(calls), 1)
        args = calls[0]
        self.assertIn('-e', args)  # server errors must be failed acknowledgements
        fields = dict(zip(args[args.index('*') + 1::2], args[args.index('*') + 2::2]))
        raw = json.loads(fields['_raw'])
        self.assertEqual(raw['data'], json.loads(fields['data']))
        self.assertEqual(raw['type'], fields['type'])
        self.assertEqual(raw['source'], fields['source'])
        self.assertEqual(raw['id'], fields['event_id'])
        self.assertEqual(raw['data']['severity'], 'critical')
        uuid.UUID(raw['id'])

    def test_failed_publish_does_not_consume_transition(self):
        states, calls = self.fixture(True)
        self.assertEqual(states, ['ok', 'critical'])
        self.assertEqual(len(calls), 2)
