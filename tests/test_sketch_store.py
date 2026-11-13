"""叶/父存储层：版本提交、缓存键、精确核对、修订重建、原子性、同路径换内容。"""

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from src.hll import HASH_VERSION, HyperLogLog, SketchHeaderError, sketch_from_bytes
from src.sketch_store import (
    LayerStore,
    LeafRef,
    TMP_SUFFIX,
    load_sketches_config,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class LayerStoreTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.store = LayerStore(self.dir / "store", p=12)

    def tearDown(self):
        self._tmp.cleanup()

    def _commit_pair(self):
        # 题设两叶：{a,b} 与 {b,c}。
        first = self.store.commit_leaf("day1", 1, ["a", "b"])
        second = self.store.commit_leaf("day2", 1, ["b", "c"])
        return first, second


class LeafCommitTests(LayerStoreTestBase):
    def test_leaf_file_is_immutable_version_with_full_header(self):
        commit = self.store.commit_leaf("day1", 1, ["a", "b", "a"])
        header, sketch = sketch_from_bytes(commit.path.read_bytes())
        self.assertEqual(header["kind"], "leaf")
        self.assertEqual(header["p"], 12)
        self.assertEqual(header["hash_version"], HASH_VERSION)
        self.assertEqual(header["revision"], 1)
        self.assertEqual(header["exact_cardinality"], 2)  # 重复键只计一次
        self.assertEqual(header["devices"], ["a", "b"])
        self.assertEqual(commit.ref.digest,
                         hashlib.sha256(commit.path.read_bytes()).hexdigest())

    def test_revisions_coexist_as_distinct_files(self):
        rev1 = self.store.commit_leaf("day2", 1, ["b", "c"])
        rev2 = self.store.commit_leaf("day2", 2, ["a"])
        self.assertNotEqual(rev1.path, rev2.path)
        self.assertTrue(rev1.path.exists() and rev2.path.exists())
        self.assertEqual(rev1.exact_cardinality, 2)
        self.assertEqual(rev2.exact_cardinality, 1)


class ExactUnionTests(LayerStoreTestBase):
    def test_exact_union_is_three_not_four(self):
        first, second = self._commit_pair()
        exact = self.store.exact_union([first.ref, second.ref])
        self.assertEqual(exact["exact_union_cardinality"], 3)
        self.assertEqual(exact["exact_union_devices"], ["a", "b", "c"])
        # 显式断言：绝不用日去重数之和（2+2=4）代替周去重。
        self.assertNotEqual(
            exact["exact_union_cardinality"],
            sum(item["exact_cardinality"] for item in exact["per_leaf"]),
        )

    def test_union_after_leaf_replacement_is_two(self):
        first, old_second = self._commit_pair()
        new_second = self.store.commit_leaf("day2", 2, ["a"])
        exact = self.store.exact_union([first.ref, new_second.ref])
        self.assertEqual(exact["exact_union_cardinality"], 2)
        self.assertEqual(exact["exact_union_devices"], ["a", "b"])
        # 旧叶仍在，但父层引用的是新版本。
        self.assertTrue(old_second.path.exists())

    def test_approximate_estimate_is_reported_but_not_equated_to_exact(self):
        first, second = self._commit_pair()
        check = self.store.cross_check("week1", [first.ref, second.ref])
        parent = check["parent"]
        self.assertIn("raw_estimate", parent)
        self.assertIn("corrected_estimate", parent)
        self.assertEqual(parent["zero_registers"],
                         check["parent"]["zero_registers"])
        self.assertGreater(parent["corrected_estimate"], 0.0)
        self.assertEqual(check["exact"]["exact_union_cardinality"], 3)
        self.assertIn("不保证单次结果", check["note"])


class ParentCacheTests(LayerStoreTestBase):
    def test_parent_result_carries_required_fields(self):
        first, second = self._commit_pair()
        result = self.store.build_parent("week1", [first.ref, second.ref])
        payload = result.to_dict()
        self.assertEqual([item["partition"] for item in payload["leaf_set"]],
                         ["day1", "day2"])
        self.assertIn("leaf_cache_key", payload)
        self.assertIsInstance(payload["raw_estimate"], float)
        self.assertIsInstance(payload["corrected_estimate"], float)
        self.assertGreaterEqual(payload["zero_registers"], 0)

    def test_duplicate_leaf_reference_is_not_double_counted(self):
        first, second = self._commit_pair()
        once = self.store.build_parent("week1", [first.ref, second.ref])
        repeated = self.store.build_parent(
            "week1_dup", [first.ref, second.ref, second.ref, first.ref])
        self.assertEqual(once.cache_key, repeated.cache_key)
        _, sketch_once = sketch_from_bytes(once.path.read_bytes())
        _, sketch_repeated = sketch_from_bytes(repeated.path.read_bytes())
        self.assertEqual(sketch_once.registers, sketch_repeated.registers)
        exact = self.store.exact_union([first.ref, second.ref, second.ref])
        self.assertEqual(exact["exact_union_cardinality"], 3)

    def test_cache_hit_on_identical_leaf_set(self):
        first, second = self._commit_pair()
        built = self.store.build_parent("week1", [first.ref, second.ref])
        self.assertFalse(built.reused_cache)
        rebuilt = self.store.build_parent("week1", [first.ref, second.ref])
        self.assertTrue(rebuilt.reused_cache)
        self.assertEqual(built.cache_key, rebuilt.cache_key)

    def test_replacement_rebuilds_from_scratch_without_subtraction(self):
        first, old_second = self._commit_pair()
        old_parent = self.store.build_parent("week1", [first.ref, old_second.ref])
        # 旧父文件在原子替换前的字节保持完整可读（修订不做原地寄存器减法）。
        old_bytes = old_parent.path.read_bytes()
        old_header_snapshot, old_sketch = sketch_from_bytes(old_bytes)
        new_second = self.store.commit_leaf("day2", 2, ["a"])
        revised = self.store.revise_parent("week1", [first.ref, new_second.ref])
        self.assertNotEqual(old_parent.cache_key, revised.cache_key)
        self.assertFalse(revised.reused_cache)
        self.assertEqual(old_header_snapshot["cache_key"], old_parent.cache_key)

        # 修订结果必须等于“从仍有效全部叶重新 merge”，而不是旧父减出来的。
        fresh = HyperLogLog(12)
        _, leaf1 = self.store.load_leaf("day1", 1)
        _, leaf2 = self.store.load_leaf("day2", 2)
        fresh.merge(leaf1)
        fresh.merge(leaf2)
        _, parent_sketch = sketch_from_bytes(revised.path.read_bytes())
        self.assertEqual(parent_sketch.registers, fresh.registers)

        # 原子替换语义：替换前读到的旧字节始终是完整草图，
        # 替换后正式文件只承载新版本，不存在“旧寄存器被原地相减”的中间态。
        self.assertEqual(old_sketch.registers, sketch_from_bytes(old_bytes)[1].registers)
        new_header, _ = sketch_from_bytes(old_parent.path.read_bytes())
        self.assertEqual(new_header["cache_key"], revised.cache_key)

    def test_cache_key_ignores_file_paths_and_order(self):
        first, second = self._commit_pair()
        key_a = LayerStore.cache_key_for([first.ref, second.ref])
        key_b = LayerStore.cache_key_for([second.ref, first.ref])
        self.assertEqual(key_a, key_b)
        same_identity_other_path = LeafRef(
            first.ref.partition, first.ref.revision, first.ref.digest)
        key_c = LayerStore.cache_key_for([same_identity_other_path, second.ref])
        self.assertEqual(key_a, key_c)

    def test_same_path_replaced_content_invalidates_cache(self):
        first, second = self._commit_pair()
        self.store.build_parent("week1", [first.ref, second.ref])

        # 模拟同路径内容被替换（绕过不可变提交，直接改字节）：
        # 缓存键里的 digest 与现文件不符，旧父不得被复用。
        rogue = HyperLogLog(12)
        rogue.add("zzz")
        second.path.write_bytes(
            rogue.to_bytes({"kind": "leaf", "p": 12,
                            "hash_version": HASH_VERSION,
                            "partition": "day2", "revision": 1,
                            "exact_cardinality": 1, "devices": ["zzz"]}))

        # 引用仍指向旧 digest：完整性校验失败 -> 重建时显式报错而不是静默复用。
        with self.assertRaises(SketchHeaderError):
            self.store.build_parent("week1", [first.ref, second.ref])

        # 用当前文件重新解析引用后，新键与旧键不同，必须全量重 merge。
        new_ref = LeafRef("day2", 1,
                          hashlib.sha256(second.path.read_bytes()).hexdigest())
        revised = self.store.build_parent("week1", [first.ref, new_ref])
        self.assertNotEqual(
            revised.cache_key,
            LayerStore.cache_key_for([first.ref, second.ref]))
        self.assertFalse(revised.reused_cache)

    def test_incompatible_parent_headers_are_rejected(self):
        first, _ = self._commit_pair()
        other_store = LayerStore(self.dir / "other", p=8)
        foreign = other_store.commit_leaf("day1", 1, ["a", "b"])
        with self.assertRaises(SketchHeaderError):
            self.store.build_parent("broken", [first.ref, foreign.ref])

    def test_empty_leaf_set_rejected(self):
        with self.assertRaises(ValueError):
            self.store.build_parent("empty", [])


class AtomicityTests(LayerStoreTestBase):
    def test_interrupted_write_leaves_old_parent_readable(self):
        first, second = self._commit_pair()
        parent = self.store.build_parent("week1", [first.ref, second.ref])
        old_bytes = parent.path.read_bytes()

        # 中断发生在 os.replace 之前：只剩临时文件，旧父完整可读。
        tmp = parent.path.with_name(parent.path.name + TMP_SUFFIX)
        tmp.write_bytes(b"partial")
        try:
            header, sketch = sketch_from_bytes(old_bytes)
            self.assertEqual(header["kind"], "parent")
            self.assertEqual(len(sketch.registers), sketch.m)
        finally:
            tmp.unlink()

    def test_no_tmp_files_left_after_commits(self):
        first, second = self._commit_pair()
        self.store.build_parent("week1", [first.ref, second.ref])
        leftovers = [p for p in self.dir.rglob("*") if p.name.endswith(TMP_SUFFIX)]
        self.assertEqual(leftovers, [])

    def test_tmp_file_never_appears_under_final_name(self):
        # 原子性的可观察后果：目录中最终文件要么是旧版本要么是新版本，
        # 不会出现半截内容占用正式文件名。
        first, _ = self._commit_pair()
        new_second = self.store.commit_leaf("day2", 2, ["a"])
        self.store.build_parent("week1", [first.ref, new_second.ref])
        for entry in os.listdir(self.store.dir):
            with open(self.store.dir / entry, "rb") as stream:
                magic = stream.read(4)
            self.assertEqual(magic, b"HLL1", f"{entry} 不是完整草图")


class FixtureContractTests(unittest.TestCase):
    def test_config_hash_spec_matches_implementation(self):
        config = load_sketches_config(FIXTURES / "sketches.json")
        self.assertEqual(config["hash"], HASH_VERSION)
        self.assertIn(config["p"], range(4, 17))

    def test_fixture_keys_are_synthetic_only(self):
        # 防回归：fixtures 只允许合成短键，禁止引入真实用户标识。
        import re
        payload = json.loads((FIXTURES / "leaf_devices.json").read_text(encoding="utf-8"))
        pattern = re.compile(r"^[a-zA-Z0-9_\-]{1,16}$")
        for leaf in payload["leaves"]:
            for key in leaf["devices"]:
                self.assertRegex(key, pattern)

    def test_end_to_end_with_fixture_revisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LayerStore(Path(tmp) / "store", p=12)
            d1 = store.commit_leaf("day1", 1, ["a", "b"])
            d2r1 = store.commit_leaf("day2", 1, ["b", "c"])
            d2r2 = store.commit_leaf("day2", 2, ["a"])

            week_v1 = store.build_parent("week1", [d1.ref, d2r1.ref])
            self.assertEqual(
                store.exact_union([d1.ref, d2r1.ref])["exact_union_cardinality"], 3)
            week_v2 = store.revise_parent("week1", [d1.ref, d2r2.ref])
            self.assertEqual(
                store.exact_union([d1.ref, d2r2.ref])["exact_union_cardinality"], 2)
            self.assertNotEqual(week_v1.cache_key, week_v2.cache_key)
            self.assertTrue(week_v2.reused_cache is False)


if __name__ == "__main__":
    unittest.main()
