#!/usr/bin/python3

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
UPDATER = APP_DIR / "bin" / "omarchy-agent-usage-kimi-update"


class UpdateTest(unittest.TestCase):
  def setUp(self):
    self.tempdir = tempfile.TemporaryDirectory()
    self.addCleanup(self.tempdir.cleanup)
    self.root = Path(self.tempdir.name)
    self.state_home = self.root / "state"
    self.output = self.state_home / "omarchy" / "agents" / "usage" / "kimi.json"
    self.args_log = self.root / "args.json"
    self.collector = self.root / "fake-collector"
    self.collector.write_text(
      "#!/usr/bin/python3\n"
      "import json, os, sys\n"
      "from pathlib import Path\n"
      "Path(os.environ['FAKE_ARGS_LOG']).write_text(json.dumps(sys.argv[1:]))\n"
      "sys.stdout.write(os.environ.get('FAKE_OUTPUT', ''))\n"
      "sys.stderr.write(os.environ.get('FAKE_ERROR', ''))\n"
      "raise SystemExit(int(os.environ.get('FAKE_EXIT', '0')))\n",
      encoding="utf-8",
    )
    self.collector.chmod(0o755)

  def run_update(self, *args, output='{"schemaVersion":1,"id":"kimi"}\n', exit_code=0):
    env = os.environ.copy()
    env.update({
      "XDG_STATE_HOME": str(self.state_home),
      "OMARCHY_KIMI_COLLECTOR": str(self.collector),
      "FAKE_ARGS_LOG": str(self.args_log),
      "FAKE_OUTPUT": output,
      "FAKE_EXIT": str(exit_code),
    })
    return subprocess.run(
      [str(UPDATER), *args],
      capture_output=True,
      text=True,
      env=env,
      timeout=10,
    )

  def test_writes_valid_kimi_record_atomically(self):
    result = self.run_update()

    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertEqual(json.loads(self.output.read_text(encoding="utf-8"))["id"], "kimi")
    self.assertEqual(list(self.output.parent.glob(".kimi.*")), [])

  def test_invalid_record_leaves_last_known_good_file_untouched(self):
    self.output.parent.mkdir(parents=True)
    self.output.write_text('{"id":"kimi","sentinel":true}\n', encoding="utf-8")

    result = self.run_update(output='{"schemaVersion":1,"id":"codex"}\n')

    self.assertNotEqual(result.returncode, 0)
    self.assertTrue(json.loads(self.output.read_text(encoding="utf-8"))["sentinel"])

  def test_malformed_json_leaves_last_known_good_file_untouched(self):
    self.output.parent.mkdir(parents=True)
    self.output.write_text('{"id":"kimi","sentinel":true}\n', encoding="utf-8")

    result = self.run_update(output="not json\n")

    self.assertNotEqual(result.returncode, 0)
    self.assertTrue(json.loads(self.output.read_text(encoding="utf-8"))["sentinel"])

  def test_collector_failure_leaves_last_known_good_file_untouched(self):
    self.output.parent.mkdir(parents=True)
    self.output.write_text('{"id":"kimi","sentinel":true}\n', encoding="utf-8")

    result = self.run_update(exit_code=7)

    self.assertEqual(result.returncode, 7)
    self.assertTrue(json.loads(self.output.read_text(encoding="utf-8"))["sentinel"])

  def test_forwards_force_and_limits_only_flags(self):
    result = self.run_update("--force", "--limits-only")

    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertEqual(json.loads(self.args_log.read_text(encoding="utf-8")), ["--force", "--limits-only"])


if __name__ == "__main__":
  unittest.main()
