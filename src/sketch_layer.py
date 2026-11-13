"""命令行入口：叶提交、父合并/修订、精确核对。

所有输出为单行 JSON。示例::

    python -m src.sketch_layer commit-leaf fixtures/leaf_devices.json day1 1 \
        --output-dir .workspace-output/store --p 12
    python -m src.sketch_layer parent week1 day1@1 day2@1 --store-dir ...
    python -m src.sketch_layer exact week1 day1@1 day2@1 --store-dir ...
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys

from .hll import ALLOWED_P
from .sketch_reader import load
from .sketch_store import LayerStore, LeafRef


def _leaf_devices(leaf_input: str, partition: str, revision: int) -> list[str]:
    payload = load(leaf_input)
    if isinstance(payload, dict) and "leaves" in payload:
        for entry in payload["leaves"]:
            if entry.get("partition") == partition and entry.get("revision") == revision:
                return list(entry["devices"])
        raise SystemExit(f"输入中找不到叶 {partition} rev{revision}")
    if isinstance(payload, list):
        return list(payload)
    raise SystemExit("无法识别的叶输入结构")


def _resolve_ref(store: LayerStore, token: str) -> LeafRef:
    if "@" not in token:
        raise SystemExit(f"叶引用 {token!r} 应为 分区@修订号 形式")
    partition, _, rev_text = token.partition("@")
    revision = int(rev_text)
    path = store.leaf_path(partition, revision)
    if not path.exists():
        raise SystemExit(f"叶文件不存在：{path}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return LeafRef(partition, revision, digest)


def _resolve_refs(store: LayerStore, tokens: list[str]) -> list[LeafRef]:
    return [_resolve_ref(store, token) for token in tokens]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="HLL 叶/父近似去重聚合层")
    sub = parser.add_subparsers(dest="command", required=True)

    commit = sub.add_parser("commit-leaf", help="提交不可变叶版本")
    commit.add_argument("leaf_input")
    commit.add_argument("partition")
    commit.add_argument("revision", type=int)
    commit.add_argument("--store-dir", required=True)
    commit.add_argument("--p", type=int, default=12, choices=sorted(ALLOWED_P))

    parent = sub.add_parser("parent", help="合并叶为父分区（命中缓存则复用）")
    parent.add_argument("parent_partition")
    parent.add_argument("leaf_refs", nargs="+", help="形如 day1@1 day2@1")
    parent.add_argument("--store-dir", required=True)
    parent.add_argument("--p", type=int, default=12, choices=sorted(ALLOWED_P))

    exact = sub.add_parser("exact", help="精确小样本核对并附近似估计")
    exact.add_argument("parent_partition")
    exact.add_argument("leaf_refs", nargs="+")
    exact.add_argument("--store-dir", required=True)
    exact.add_argument("--p", type=int, default=12, choices=sorted(ALLOWED_P))

    revise = sub.add_parser("revise", help="叶替换后从有效叶重建父层")
    revise.add_argument("parent_partition")
    revise.add_argument("leaf_refs", nargs="+")
    revise.add_argument("--store-dir", required=True)
    revise.add_argument("--p", type=int, default=12, choices=sorted(ALLOWED_P))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = LayerStore(args.store_dir, args.p)

    if args.command == "commit-leaf":
        devices = _leaf_devices(args.leaf_input, args.partition, args.revision)
        commit = store.commit_leaf(args.partition, args.revision, devices)
        result = {
            "committed_leaf": {
                "partition": commit.ref.partition,
                "revision": commit.ref.revision,
                "digest": commit.ref.digest,
                "path": str(commit.path),
                "exact_cardinality": commit.exact_cardinality,
            }
        }
    elif args.command in ("parent", "revise"):
        refs = _resolve_refs(store, args.leaf_refs)
        result = store.build_parent(args.parent_partition, refs).to_dict()
        result["command"] = args.command
    elif args.command == "exact":
        refs = _resolve_refs(store, args.leaf_refs)
        result = store.cross_check(args.parent_partition, refs)
    else:  # pragma: no cover - argparse 已保证
        raise SystemExit(f"未知命令 {args.command}")

    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
