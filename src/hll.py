"""HyperLogLog 近似去重核心。

哈希规范（固定，不允许静默更换）：
    对设备键 ``key`` 取 ``hashlib.sha256(key.encode("utf-8"))`` 的前 8 个字节，
    按大端序解释为 64 位无符号整数。哈希版本字符串
    ``sha256_utf8_first64_bigendian`` 必须写入每个草图头。

寄存器寻址：
    * 高 ``p`` 位作为寄存器下标（共 m = 2**p 个寄存器）；
    * 剩余 64-p 位计算 rank：前导零个数 + 1；
    * 余串全零时 rank = 65-p（不是 64-p，避免与“见到 1 才结束”的情形混淆）。

合并：同下标寄存器逐位取 max，只允许 p 与哈希版本完全相同的草图合并。

估计：标准 HLL 调和均值估计，小范围（raw <= 2.5m 且存在零寄存器）切换
linear counting。本模块使用 64 位哈希，不做经典 32 位实现中的大范围
饱和修正。相对误差 ~1.04/sqrt(m) 是渐近统计口径，不代表每个单次结果
都落在该理论相对误差之内。
"""

from __future__ import annotations

import hashlib
import json
import math

#: 固定哈希版本标识，草图头必须原样写入该值。
HASH_VERSION = "sha256_utf8_first64_bigendian"

#: 正式支持的精度集合（m = 2**p）。p=2（m=4）不在其中，
#: 长度为 4 的寄存器数组仅用于 max 代数的独立单元核对。
ALLOWED_P = frozenset(range(4, 17))

#: 落盘二进制格式魔数与版本。
MAGIC = b"HLL1"

#: 64 位哈希的总位宽。
HASH_BITS = 64


class SketchHeaderError(ValueError):
    """草图头不兼容或落盘字节流无法解析。"""


def hash64(key: str) -> int:
    """按固定规范把设备键散列为 64 位非负整数。"""
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def index_and_rank(h: int, p: int) -> tuple[int, int]:
    """给定 64 位哈希与精度，返回 (寄存器下标, rank)。

    高 p 位选寄存器；剩余 64-p 位的前导零数加 1 为 rank，
    余串全零时 rank 为 65-p。
    """
    if not (0 <= h < 1 << HASH_BITS):
        raise ValueError("哈希值必须是 64 位非负整数")
    tail_bits = HASH_BITS - p
    index = h >> tail_bits
    tail = h & ((1 << tail_bits) - 1)
    if tail == 0:
        rank = tail_bits + 1  # = 65 - p
    else:
        rank = tail_bits - tail.bit_length() + 1
    return index, rank


def register_max(left: list[int], right: list[int]) -> list[int]:
    """逐位 max 代数（纯函数，不要求数组长度对应某个正式 p）。"""
    if len(left) != len(right):
        raise SketchHeaderError("寄存器长度不一致，无法逐位合并")
    return [a if a >= b else b for a, b in zip(left, right)]


def alpha_m(m: int) -> float:
    """标准 HLL alpha_m 常数：m=16/32/64 专用值，其余走通用公式。"""
    if m == 16:
        return 0.673
    if m == 32:
        return 0.697
    if m == 64:
        return 0.709
    return 0.7213 / (1.0 + 1.079 / m)


class HyperLogLog:
    """不可变语义下的 HLL 草图（叶与父共用同一寄存器表示）。"""

    def __init__(self, p: int, hash_version: str = HASH_VERSION,
                 registers: list[int] | None = None) -> None:
        if p not in ALLOWED_P:
            raise ValueError(f"p={p} 不是受支持的正式精度（允许 4..16）")
        if not isinstance(hash_version, str) or not hash_version:
            raise ValueError("哈希版本必须是非空字符串")
        self.p = p
        self.m = 1 << p
        self.hash_version = hash_version
        max_rank = HASH_BITS - p + 1  # 65 - p
        if registers is None:
            self.registers = [0] * self.m
        else:
            regs = list(registers)
            if len(regs) != self.m:
                raise SketchHeaderError(
                    f"寄存器数 {len(regs)} 与 m={self.m} 不符")
            if any(not (0 <= r <= max_rank) for r in regs):
                raise SketchHeaderError(f"寄存器取值必须落在 0..{max_rank}")
            self.registers = regs

    def add_hash(self, h: int) -> None:
        index, rank = index_and_rank(h, self.p)
        if rank > self.registers[index]:
            self.registers[index] = rank

    def add(self, key: str) -> None:
        self.add_hash(hash64(key))

    def merge(self, other: "HyperLogLog") -> None:
        """就地合并另一草图；p 或哈希版本不同一律拒绝，不做对齐换算。"""
        if not isinstance(other, HyperLogLog):
            raise TypeError("只能合并 HyperLogLog 草图")
        if self.p != other.p:
            raise SketchHeaderError(
                f"精度不兼容：p={self.p} 与 p={other.p}，禁止对齐成同型")
        if self.hash_version != other.hash_version:
            raise SketchHeaderError(
                f"哈希版本不兼容：{self.hash_version!r} 与 "
                f"{other.hash_version!r}，禁止合并")
        self.registers = register_max(self.registers, other.registers)

    def estimate(self) -> dict:
        """返回原始估计、校正估计、零寄存器数与所用常数/切换信息。

        公式与适用范围：
          raw = alpha_m * m**2 / sum(2**-Rj)
          当 raw <= 2.5*m 且零寄存器数 V > 0（小范围）：
              corrected = m * ln(m / V)            （linear counting）
          否则 corrected = raw。
        64 位哈希空间下不做 32 位经典实现的大范围修正。
        """
        m = self.m
        alpha = alpha_m(m)
        zero = sum(1 for rank in self.registers if rank == 0)
        # 空集在切换公式之前直接判定；否则 raw=0 会误满足 raw<=2.5m。
        if zero == m:
            return {
                "p": self.p,
                "m": m,
                "alpha_m": alpha,
                "raw_estimate": 0.0,
                "corrected_estimate": 0.0,
                "zero_registers": zero,
                "small_range_linear_counting": False,
                "hash_version": self.hash_version,
            }
        harmonic = sum(2.0 ** (-rank) for rank in self.registers)
        raw = alpha * m * m / harmonic
        small_range = raw <= 2.5 * m and zero > 0
        if small_range:
            corrected = m * math.log(m / zero)
        else:
            corrected = raw
        return {
            "p": self.p,
            "m": m,
            "alpha_m": alpha,
            "raw_estimate": raw,
            "corrected_estimate": corrected,
            "zero_registers": zero,
            "small_range_linear_counting": small_range,
            "hash_version": self.hash_version,
        }

    def to_bytes(self, header: dict) -> bytes:
        """序列化为 ``魔数 | 头长度(4B大端) | JSON头 | 寄存器(m字节)``。

        调用方必须在 header 中写入 p 与 hash_version；本方法强制复核，
        防止头与寄存器实际参数脱节。
        """
        if header.get("p") != self.p:
            raise SketchHeaderError("草图头 p 与寄存器精度不一致")
        if header.get("hash_version") != self.hash_version:
            raise SketchHeaderError("草图头 hash_version 与草图不一致")
        payload = json.dumps(header, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return MAGIC + len(payload).to_bytes(4, "big") + payload + bytes(self.registers)


def sketch_from_bytes(data: bytes) -> tuple[dict, HyperLogLog]:
    """解析落盘草图，返回 (头字典, 草图)。任何结构问题抛 SketchHeaderError。"""
    if len(data) < len(MAGIC) + 4 or not data.startswith(MAGIC):
        raise SketchHeaderError("魔数不匹配或字节流过短，不是本格式草图")
    offset = len(MAGIC)
    header_len = int.from_bytes(data[offset:offset + 4], "big")
    offset += 4
    if header_len == 0 or offset + header_len > len(data):
        raise SketchHeaderError("草图头长度越界")
    try:
        header = json.loads(data[offset:offset + header_len].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SketchHeaderError(f"草图头不是合法 UTF-8 JSON：{exc}") from exc
    if not isinstance(header, dict):
        raise SketchHeaderError("草图头必须是 JSON 对象")
    p = header.get("p")
    hash_version = header.get("hash_version")
    if not isinstance(p, int) or p not in ALLOWED_P:
        raise SketchHeaderError("草图头缺少受支持的整数 p")
    if not isinstance(hash_version, str) or not hash_version:
        raise SketchHeaderError("草图头缺少哈希版本")
    offset += header_len
    m = 1 << p
    regs = list(data[offset:offset + m])
    if offset + m != len(data):
        raise SketchHeaderError("寄存器字节长度与 m 不符或存在尾随垃圾字节")
    return header, HyperLogLog(p, hash_version, regs)
