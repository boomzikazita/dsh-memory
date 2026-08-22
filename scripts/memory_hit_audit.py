#!/usr/bin/env python3
"""memory_hit_audit.py — 记忆命中审计（借鉴 hermes memory_hits：存了不用要有度量）

统计 rrf_recall.py record_hits 落库的 hits.sqlite，反查 L1 条目命中情况：

  1. L1 从未命中清单（超预算砍条 / 蒸馏淘汰的依据）
  2. 命中 TOP N（高复用条目）
  3. 命中率汇总（已命中 / 未命中 / 总量）

用法:
  memory_hit_audit.py           全部输出
  memory_hit_audit.py --never   仅从未命中清单（供周审 prompt 引用）
  memory_hit_audit.py --top 10  TOP N 命中
"""
import argparse
import pathlib
import sqlite3

MEMORIES = pathlib.Path.home() / ".dsh" / "memories"
HITS_DB = MEMORIES / "hits.sqlite"
L1_FILES = ["MEMORY.md", "USER.md"]


def split_entries(text: str) -> list:
    return [e.strip() for e in text.split("§") if e.strip()]


def load_l1_entries() -> list:
    out = []
    for fname in L1_FILES:
        f = MEMORIES / fname
        if not f.exists():
            continue
        for i, ent in enumerate(split_entries(f.read_text(encoding="utf-8"))):
            out.append({"file": fname, "idx": i, "text": ent})
    return out


def load_hits() -> dict:
    """lane -> {key: {text, count, last_ts}}"""
    if not HITS_DB.exists():
        return {}
    con = sqlite3.connect(f"file:{HITS_DB}?mode=ro", uri=True)
    out = {}
    try:
        for lane, key, text, count, last_ts in con.execute(
            "SELECT lane, key, text, count, last_ts FROM hits"
        ):
            out.setdefault(lane, {})[key] = {"text": text, "count": count, "last_ts": last_ts}
    finally:
        con.close()
    return out


def entry_hit(entry: str, lane_hits: dict) -> tuple:
    """L1 条目是否被命中：hits.text 是条目文本的前缀（record_hits 存 text[:200]）。
    返回 (hit, count)。前缀匹配 + 包含匹配双保险。"""
    head = entry[:200]
    for h in lane_hits.values():
        ht = h["text"]
        if head == ht or ht in entry or entry[: len(ht)] == ht:
            return True, h["count"]
    return False, 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--never", action="store_true", help="仅输出 L1 从未命中清单")
    ap.add_argument("--top", type=int, default=10, help="TOP N 命中（默认 10）")
    args = ap.parse_args()

    hits = load_hits()
    l1_hits = hits.get("L1", {})
    entries = load_l1_entries()

    never = []
    hit_stats = []
    for e in entries:
        hit, count = entry_hit(e["text"], l1_hits)
        if hit:
            hit_stats.append((count, e["file"], e["idx"], e["text"]))
        else:
            never.append((e["file"], e["idx"], e["text"]))

    if args.never:
        print(f"# L1 从未命中条目（共 {len(never)}/{len(entries)}，{100*len(never)//max(len(entries),1)}%）")
        for fname, idx, text in never:
            print(f"- [{fname}:{idx}] {text[:120]}")
        return

    print(f"# 记忆命中审计  L1 总量 {len(entries)} · 已命中 {len(hit_stats)} · 从未命中 {len(never)}")
    print()
    print(f"## 命中 TOP {args.top}")
    for count, fname, idx, text in sorted(hit_stats, key=lambda x: -x[0])[: args.top]:
        print(f"- ({count} 次) [{fname}:{idx}] {text[:100]}")
    print()
    print(f"## 从未命中（{len(never)} 条——L1 预算淘汰/蒸馏候选）")
    for fname, idx, text in never:
        print(f"- [{fname}:{idx}] {text[:120]}")
    print()
    print("# 各层命中总量")
    for lane in sorted(hits.keys()):
        total = sum(h["count"] for h in hits[lane].values())
        print(f"- {lane}: {len(hits[lane])} 条 / 累计 {total} 次")


if __name__ == "__main__":
    main()
