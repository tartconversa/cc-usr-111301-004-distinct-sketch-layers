"""CLI 冒烟测试：进程内调用 + 隔离模式子进程读取 fixtures。"""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from src.sketch_layer import main

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"


class CliSmokeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store_dir = Path(self._tmp.name) / "store"

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, *argv) -> dict:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(list(argv))
        self.assertEqual(code, 0)
        return json.loads(buf.getvalue())

    def test_commit_parent_exact_revise_flow(self):
        leaf_input = str(FIXTURES / "leaf_devices.json")
        committed = self._run("commit-leaf", leaf_input, "day1", "1",
                              "--store-dir", str(self.store_dir), "--p", "12")
        self.assertEqual(committed["committed_leaf"]["exact_cardinality"], 2)
        self._run("commit-leaf", leaf_input, "day2", "1",
                  "--store-dir", str(self.store_dir), "--p", "12")
        self._run("commit-leaf", leaf_input, "day2", "2",
                  "--store-dir", str(self.store_dir), "--p", "12")

        parent = self._run("parent", "week1", "day1@1", "day2@1",
                           "--store-dir", str(self.store_dir))
        self.assertEqual(len(parent["leaf_set"]), 2)
        self.assertFalse(parent["reused_cache"])
        cached = self._run("parent", "week1", "day1@1", "day2@1",
                           "--store-dir", str(self.store_dir))
        self.assertTrue(cached["reused_cache"])

        exact = self._run("exact", "week1", "day1@1", "day2@1",
                          "--store-dir", str(self.store_dir))
        self.assertEqual(exact["exact"]["exact_union_cardinality"], 3)

        revised = self._run("revise", "week1", "day1@1", "day2@2",
                            "--store-dir", str(self.store_dir))
        exact_after = self._run("exact", "week1", "day1@1", "day2@2",
                                "--store-dir", str(self.store_dir))
        self.assertNotEqual(revised["leaf_cache_key"], cached["leaf_cache_key"])
        self.assertEqual(exact_after["exact"]["exact_union_cardinality"], 2)

    def test_subprocess_isolated_mode_reads_fixtures(self):
        # -I 隔离模式：从临时 cwd 运行，显式把仓库根放入 sys.path，
        # 验证读取不受当前目录污染影响。
        env = dict(os.environ)
        code = (
            "import sys; sys.path.insert(0, %r);"
            "from src.sketch_layer import main;"
            "sys.exit(main(['commit-leaf', %r, 'day1', '1',"
            " '--store-dir', %r, '--p', '12']))"
        ) % (str(ROOT), str(FIXTURES / "leaf_devices.json"), str(self.store_dir))
        with tempfile.TemporaryDirectory() as cwd:
            proc = subprocess.run(
                [sys.executable, "-I", "-c", code],
                cwd=cwd, env=env, capture_output=True, text=True, check=True)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["committed_leaf"]["partition"], "day1")


if __name__ == "__main__":
    unittest.main()
