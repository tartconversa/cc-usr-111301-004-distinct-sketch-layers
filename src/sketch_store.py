"""叶草图版本提交与父分区合并缓存。

设计要点
========
* 叶是不可变版本：``leaf_<分区>_rev<修订号>.hll`` 一经写出不再修改；
  分区更正以新 revision 的新文件表达，叶头保存精确设备键集合，
  用于小样本精确核对。
* 父层是可重建缓存：``parent_<分区>.hll`` 的缓存键由
  规范化（去重、排序）后的叶版本引用集合构成，引用身份是
  (分区, revision, 内容 sha256)，与文件路径无关——同路径换内容后
  旧缓存不会被复用。
* 叶被替换后**不允许**对父寄存器做减法：修订就是从当前仍有效的
  全部叶重新 merge，原子写入新缓存；写入中断时旧父文件保持完整可读。
* 重复叶引用在规范化阶段折叠，绝不双计。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from .hll import (
    ALLOWED_P,
    HASH_VERSION,
    HyperLogLog,
    SketchHeaderError,
    sketch_from_bytes,
)

LEAF_PREFIX = "leaf_"
PARENT_PREFIX = "parent_"
SUFFIX = ".hll"
TMP_SUFFIX = ".tmp"


@dataclass(frozen=True)
class LeafRef:
    """叶版本的规范化引用身份（不含文件路径）。"""

    partition: str
    revision: int
    digest: str  # 叶文件完整内容的 sha256 十六进制摘要

    def key(self) -> str:
        return f"{self.partition}@rev{self.revision}:{self.digest}"


@dataclass(frozen=True)
class LeafCommit:
    """一次叶提交的落盘结果与精确事实。"""

    ref: LeafRef
    path: Path
    p: int
    hash_version: str
    exact_cardinality: int
    devices: frozenset


@dataclass(frozen=True)
class ParentResult:
    """父分区结果：叶集合摘要 + 原始/校正估计 + 零寄存器数。"""

    partition: str
    p: int
    hash_version: str
    leaf_refs: tuple[LeafRef, ...]
    cache_key: str
    raw_estimate: float
    corrected_estimate: float
    zero_registers: int
    small_range_linear_counting: bool
    path: Path
    reused_cache: bool

    def to_dict(self) -> dict:
        return {
            "partition": self.partition,
            "p": self.p,
            "hash_version": self.hash_version,
            "leaf_set": [
                {"partition": r.partition, "revision": r.revision, "digest": r.digest}
                for r in self.leaf_refs
            ],
            "leaf_cache_key": self.cache_key,
            "raw_estimate": self.raw_estimate,
            "corrected_estimate": self.corrected_estimate,
            "zero_registers": self.zero_registers,
            "small_range_linear_counting": self.small_range_linear_counting,
            "path": os.fspath(self.path),
            "reused_cache": self.reused_cache,
        }


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    """同目录临时文件 + os.replace 原子进入；中断后目标文件保持原状。"""
    tmp = path.with_name(path.name + TMP_SUFFIX)
    try:
        with tmp.open("wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _safe_partition(name: str) -> str:
    """把分区名压成安全文件名片段，同时保留可读性。"""
    cleaned = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in name)
    if not cleaned:
        raise ValueError("分区名不能为空")
    return cleaned


class LayerStore:
    """管理一个目录下的不可变叶草图与可重建父缓存。"""

    def __init__(self, directory: str | os.PathLike, p: int) -> None:
        self.dir = Path(directory)
        self.p = p
        self.dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ 叶

    def leaf_path(self, partition: str, revision: int) -> Path:
        return self.dir / f"{LEAF_PREFIX}{_safe_partition(partition)}_rev{revision}{SUFFIX}"

    def commit_leaf(self, partition: str, revision: int,
                    devices) -> LeafCommit:
        """提交一个不可变叶版本。重复设备键只计一次。"""
        device_set = frozenset(devices)
        sketch = HyperLogLog(self.p)
        for key in device_set:
            sketch.add(key)
        header = {
            "kind": "leaf",
            "partition": partition,
            "revision": revision,
            "p": self.p,
            "hash_version": HASH_VERSION,
            "exact_cardinality": len(device_set),
            # 合成键集合，供精确核对；不包含任何真实用户标识。
            "devices": sorted(device_set),
        }
        path = self.leaf_path(partition, revision)
        _atomic_write(path, sketch.to_bytes(header))
        return LeafCommit(
            ref=LeafRef(partition, revision, _sha256_file(path)),
            path=path,
            p=self.p,
            hash_version=HASH_VERSION,
            exact_cardinality=len(device_set),
            devices=device_set,
        )

    def load_leaf(self, partition: str, revision: int) -> tuple[dict, HyperLogLog]:
        path = self.leaf_path(partition, revision)
        header, sketch = sketch_from_bytes(path.read_bytes())
        if header.get("kind") != "leaf" or header.get("partition") != partition \
                or header.get("revision") != revision:
            raise SketchHeaderError("叶草图头身份与请求的分区/修订不符")
        return header, sketch

    # ------------------------------------------------------------------ 父

    @staticmethod
    def normalize_refs(refs) -> tuple[LeafRef, ...]:
        """按引用身份去重并排序：重复叶引用折叠，绝不双计。"""
        unique = {LeafRef(r.partition, r.revision, r.digest): None for r in refs}
        return tuple(sorted(unique, key=lambda r: r.key()))

    @staticmethod
    def cache_key_for(refs) -> str:
        normalized = LayerStore.normalize_refs(refs)
        joined = "|".join(r.key() for r in normalized)
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()

    def parent_path(self, parent_partition: str) -> Path:
        return self.dir / f"{PARENT_PREFIX}{_safe_partition(parent_partition)}{SUFFIX}"

    def _read_parent_if_valid(self, path: Path, expected_key: str):
        if not path.exists():
            return None
        try:
            header, sketch = sketch_from_bytes(path.read_bytes())
        except SketchHeaderError:
            # 旧缓存损坏不影响重建，按未命中处理。
            return None
        if header.get("kind") != "parent" or header.get("cache_key") != expected_key:
            return None  # 叶集合已变（含同路径换内容），旧缓存作废。
        return header, sketch

    def _refs_intact(self, normalized: tuple[LeafRef, ...]) -> bool:
        """逐叶复核当前文件摘要；同路径换内容会在此被识别。"""
        for ref in normalized:
            path = self.leaf_path(ref.partition, ref.revision)
            if not path.exists() or _sha256_file(path) != ref.digest:
                return False
        return True

    def build_parent(self, parent_partition: str, refs) -> ParentResult:
        """从当前仍有效的全部叶重新 merge 生成/复用父缓存。

        永远从叶重建，不对旧父寄存器做任何减法；缓存命中也必须通过
        叶文件摘要复核，不会只因路径相同就复用旧父。
        """
        normalized = self.normalize_refs(refs)
        if not normalized:
            raise ValueError("父分区至少需要一个叶引用")
        key = self.cache_key_for(normalized)
        path = self.parent_path(parent_partition)

        cached = self._read_parent_if_valid(path, key)
        if cached is not None and self._refs_intact(normalized):
            header, sketch = cached
            stats = sketch.estimate()
            return ParentResult(
                partition=parent_partition,
                p=sketch.p,
                hash_version=sketch.hash_version,
                leaf_refs=normalized,
                cache_key=key,
                raw_estimate=stats["raw_estimate"],
                corrected_estimate=stats["corrected_estimate"],
                zero_registers=stats["zero_registers"],
                small_range_linear_counting=stats["small_range_linear_counting"],
                path=path,
                reused_cache=True,
            )

        merged: HyperLogLog | None = None
        leaf_summaries = []
        for ref in normalized:
            leaf_header, leaf_sketch = self.load_leaf(ref.partition, ref.revision)
            actual_digest = _sha256_file(self.leaf_path(ref.partition, ref.revision))
            if actual_digest != ref.digest:
                # 同路径内容被替换：身份失配，拒绝使用，强制上层重新解析引用。
                raise SketchHeaderError(
                    f"叶 {ref.partition} rev{ref.revision} 内容摘要已变化，"
                    "引用身份失效，请基于当前叶重建引用集合")
            if merged is None:
                merged = HyperLogLog(leaf_sketch.p, leaf_sketch.hash_version)
            merged.merge(leaf_sketch)  # 头不兼容在此显式抛错
            leaf_summaries.append({
                "partition": ref.partition,
                "revision": ref.revision,
                "digest": ref.digest,
                "exact_cardinality": leaf_header.get("exact_cardinality"),
            })

        assert merged is not None
        stats = merged.estimate()
        header = {
            "kind": "parent",
            "partition": parent_partition,
            "p": merged.p,
            "hash_version": merged.hash_version,
            "cache_key": key,
            "leaf_refs": [
                {"partition": r.partition, "revision": r.revision, "digest": r.digest}
                for r in normalized
            ],
            "leaf_summaries": leaf_summaries,
            "raw_estimate": stats["raw_estimate"],
            "corrected_estimate": stats["corrected_estimate"],
            "zero_registers": stats["zero_registers"],
        }
        _atomic_write(path, merged.to_bytes(header))  # 中断后旧父仍完整可读
        return ParentResult(
            partition=parent_partition,
            p=merged.p,
            hash_version=merged.hash_version,
            leaf_refs=normalized,
            cache_key=key,
            raw_estimate=stats["raw_estimate"],
            corrected_estimate=stats["corrected_estimate"],
            zero_registers=stats["zero_registers"],
            small_range_linear_counting=stats["small_range_linear_counting"],
            path=path,
            reused_cache=False,
        )

    def revise_parent(self, parent_partition: str, refs) -> ParentResult:
        """父层修订：叶替换后从仍有效的全部叶重新 merge（不做寄存器减法）。"""
        return self.build_parent(parent_partition, refs)

    # ------------------------------------------------------------- 精确核对

    def exact_union(self, refs) -> dict:
        """基于叶头保存的设备键集合求精确并集（小样本核对用）。"""
        normalized = self.normalize_refs(refs)
        union: set[str] = set()
        per_leaf = []
        for ref in normalized:
            header, _ = self.load_leaf(ref.partition, ref.revision)
            devices = frozenset(header.get("devices", ()))
            union |= devices
            per_leaf.append({
                "partition": ref.partition,
                "revision": ref.revision,
                "exact_cardinality": len(devices),
            })
        return {
            "leaf_refs": [
                {"partition": r.partition, "revision": r.revision, "digest": r.digest}
                for r in normalized
            ],
            "per_leaf": per_leaf,
            "exact_union_cardinality": len(union),
            "exact_union_devices": sorted(union),
        }

    def cross_check(self, parent_partition: str, refs) -> dict:
        """同一叶集合下精确并集与父近似估计并列返回，不把近似值当精确值。"""
        exact = self.exact_union(refs)
        parent = self.build_parent(parent_partition, refs).to_dict()
        return {
            "parent": parent,
            "exact": exact,
            "note": "近似估计为统计量，不保证单次结果落在理论相对误差内",
        }


def load_sketches_config(path: str | os.PathLike) -> dict:
    """读取 sketches.json 并核对其中的哈希规范与本实现一致。"""
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    if config.get("hash") != HASH_VERSION:
        raise SketchHeaderError(
            f"配置哈希规范 {config.get('hash')!r} 与实现固定规范不一致")
    if config.get("p") not in ALLOWED_P:
        raise SketchHeaderError("配置 p 不在受支持精度范围内")
    return config
