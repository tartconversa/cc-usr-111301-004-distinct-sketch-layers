"""HLL 核心：哈希黄金向量、index/rank、max 代数、估计器、头不兼容、落盘。"""

import hashlib
import math
import unittest

from src.hll import (
    ALLOWED_P,
    HASH_BITS,
    HASH_VERSION,
    MAGIC,
    HyperLogLog,
    SketchHeaderError,
    alpha_m,
    hash64,
    index_and_rank,
    register_max,
    sketch_from_bytes,
)

# 由独立 hashlib 计算并人工核对的黄金向量（SHA256 前 8 字节大端）。
GOLDEN = {
    "": ("e3b0c44298fc1c14", 16406829232824261652),
    "a": ("ca978112ca1bbdca", 14598278634844962250),
    "b": ("3e23e8160039594a", 4477677635727087946),
    "c": ("2e7d2c03a9507ae2", 3349882092070664930),
    "abc": ("ba7816bf8f01cfea", 13436514500253700074),
    "设备α": ("6e55951cfa382c14", 7950424668882086932),
}


class HashGoldenVectorTests(unittest.TestCase):
    def test_golden_hash64_vectors(self):
        for key, (prefix_hex, expected) in GOLDEN.items():
            with self.subTest(key=key):
                got = hash64(key)
                self.assertEqual(got, expected)
                # 独立重算，防止实现与黄金值同源错误。
                independent = int.from_bytes(
                    hashlib.sha256(key.encode("utf-8")).digest()[:8], "big")
                self.assertEqual(got, independent)
                self.assertEqual(f"{got:016x}", prefix_hex)

    def test_utf8_bytes_are_the_hash_input(self):
        multibyte = "设备α"
        self.assertEqual(
            hash64(multibyte),
            int.from_bytes(hashlib.sha256(multibyte.encode("utf-8")).digest()[:8], "big"),
        )
        self.assertNotEqual(hash64("a"), hash64("A"))


class IndexRankTests(unittest.TestCase):
    def test_index_rank_p4_golden(self):
        # p=4：高 4 位为下标，余 60 位前导零数 + 1。
        self.assertEqual(index_and_rank(hash64("a"), 4), (0xC, 1))
        self.assertEqual(index_and_rank(hash64("b"), 4), (0x3, 1))
        self.assertEqual(index_and_rank(hash64("c"), 4), (0x2, 1))
        self.assertEqual(index_and_rank(hash64("abc"), 4), (0xB, 1))

    def test_index_consistency_across_p(self):
        h = hash64("设备α")
        for p in ALLOWED_P:
            index, rank = index_and_rank(h, p)
            self.assertTrue(0 <= index < 1 << p)
            self.assertTrue(1 <= rank <= HASH_BITS - p + 1)
            # 下标就是高 p 位。
            self.assertEqual(index, h >> (HASH_BITS - p))

    def test_rank_of_all_zero_tail_is_65_minus_p(self):
        # 哈希 0：高 p 位下标为 0，余串全零 -> rank = 65-p。
        for p in (4, 8, 12, 16):
            index, rank = index_and_rank(0, p)
            self.assertEqual((index, rank), (0, HASH_BITS - p + 1))

    def test_rank_leading_zeros_manual(self):
        # 手工构造：p=8，余下 56 位为 0...01（仅最低位为 1）。
        p = 8
        h = 1  # 高 8 位为 0，余串有 55 个前导零
        index, rank = index_and_rank(h, p)
        self.assertEqual(index, 0)
        self.assertEqual(rank, 56)


class RegisterAlgebraTests(unittest.TestCase):
    def test_reference_max_example(self):
        # 独立寄存器单元例：只核对 max 代数，m=4 不是正式 p。
        merged = register_max([1, 0, 3, 2], [0, 2, 1, 2])
        self.assertEqual(merged, [1, 2, 3, 2])

    def test_merge_is_elementwise_max_and_idempotent(self):
        first = HyperLogLog(4)
        second = HyperLogLog(4)
        for key in ("a", "b"):
            first.add(key)
        for key in ("b", "c"):
            second.add(key)
        snapshot = list(first.registers)
        first.merge(second)
        merged_once = list(first.registers)
        # 重复 merge 幂等：不会双计。
        first.merge(second)
        self.assertEqual(first.registers, merged_once)
        self.assertTrue(all(r >= s for r, s in zip(merged_once, snapshot)))

    def test_m4_is_not_a_formal_precision(self):
        with self.assertRaises(ValueError):
            HyperLogLog(2)
        # 但纯 max 代数允许任意等长数组。
        self.assertEqual(register_max([0, 0, 0, 0], [5, 1, 0, 2]), [5, 1, 0, 2])

    def test_merge_rejects_different_p(self):
        small = HyperLogLog(4)
        large = HyperLogLog(5)
        with self.assertRaises(SketchHeaderError):
            small.merge(large)

    def test_merge_rejects_different_hash_version(self):
        left = HyperLogLog(6, hash_version=HASH_VERSION)
        right = HyperLogLog(6, hash_version="other-experiment-v1")
        with self.assertRaises(SketchHeaderError):
            left.merge(right)

    def test_no_alignment_across_incompatible_headers(self):
        left = HyperLogLog(6)
        right = HyperLogLog(6, hash_version="other-experiment-v1")
        with self.assertRaises(SketchHeaderError):
            left.merge(right)
        # 左草图保持原状，没有被“强行对齐成同型”。
        self.assertEqual(left.registers, [0] * left.m)


class EstimatorTests(unittest.TestCase):
    def test_alpha_m_constants_and_formula(self):
        self.assertEqual(alpha_m(16), 0.673)
        self.assertEqual(alpha_m(32), 0.697)
        self.assertEqual(alpha_m(64), 0.709)
        self.assertAlmostEqual(alpha_m(128), 0.7213 / (1.0 + 1.079 / 128))
        self.assertAlmostEqual(alpha_m(4096), 0.7213 / (1.0 + 1.079 / 4096))

    def test_empty_set_estimate_is_zero(self):
        sketch = HyperLogLog(12)
        stats = sketch.estimate()
        self.assertEqual(stats["zero_registers"], sketch.m)
        self.assertEqual(stats["raw_estimate"], 0.0)
        self.assertEqual(stats["corrected_estimate"], 0.0)
        self.assertFalse(stats["small_range_linear_counting"])

    def test_small_range_switches_to_linear_counting(self):
        # 手工寄存器：已知零寄存器数 V，直接核对公式与切换条件。
        p = 6
        sketch = HyperLogLog(p)
        sketch.add("a")  # 只放一个键，必然小范围
        stats = sketch.estimate()
        v = stats["zero_registers"]
        self.assertGreater(v, 0)
        self.assertTrue(stats["small_range_linear_counting"])
        self.assertAlmostEqual(
            stats["corrected_estimate"], sketch.m * math.log(sketch.m / v))
        self.assertLessEqual(stats["raw_estimate"], 2.5 * sketch.m)

    def test_raw_estimate_formula_matches_constants(self):
        import math
        sketch = HyperLogLog(4)
        for key in ("a", "b", "c", "abc"):
            sketch.add(key)
        stats = sketch.estimate()
        harmonic = sum(2.0 ** (-r) for r in sketch.registers)
        expected_raw = alpha_m(sketch.m) * sketch.m ** 2 / harmonic
        self.assertAlmostEqual(stats["raw_estimate"], expected_raw)
        self.assertTrue(math.isfinite(stats["corrected_estimate"]))
        self.assertEqual(stats["zero_registers"], sum(1 for r in sketch.registers if r == 0))


class SerializationTests(unittest.TestCase):
    def test_round_trip_carries_p_and_hash_version(self):
        sketch = HyperLogLog(10)
        for key in ("a", "b", "c"):
            sketch.add(key)
        header = {"kind": "leaf", "p": 10, "hash_version": HASH_VERSION, "partition": "d"}
        blob = sketch.to_bytes(header)
        self.assertTrue(blob.startswith(MAGIC))
        restored_header, restored = sketch_from_bytes(blob)
        self.assertEqual(restored.registers, sketch.registers)
        self.assertEqual(restored_header["hash_version"], HASH_VERSION)
        self.assertEqual(restored_header["p"], 10)

    def test_to_bytes_rejects_header_mismatch(self):
        sketch = HyperLogLog(8)
        with self.assertRaises(SketchHeaderError):
            sketch.to_bytes({"p": 9, "hash_version": HASH_VERSION})
        with self.assertRaises(SketchHeaderError):
            sketch.to_bytes({"p": 8, "hash_version": "other"})

    def test_malformed_blobs_raise_header_error(self):
        for blob in (b"", b"not-a-sketch", MAGIC + b"\x00\x00", b"HLL0" + b"\x00" * 8):
            with self.subTest(blob=blob[:8]):
                with self.assertRaises(SketchHeaderError):
                    sketch_from_bytes(blob)

    def test_trailing_bytes_are_rejected(self):
        sketch = HyperLogLog(4)
        blob = sketch.to_bytes({"p": 4, "hash_version": HASH_VERSION}) + b"\x00"
        with self.assertRaises(SketchHeaderError):
            sketch_from_bytes(blob)

    def test_unsupported_p_in_header_rejected(self):
        # p=2（m=4）不得作为正式草图落盘。
        import json
        header = json.dumps({"p": 2, "hash_version": HASH_VERSION}).encode()
        blob = MAGIC + len(header).to_bytes(4, "big") + header + bytes(4)
        with self.assertRaises(SketchHeaderError):
            sketch_from_bytes(blob)


if __name__ == "__main__":
    unittest.main()
