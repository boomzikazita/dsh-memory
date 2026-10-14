#!/usr/bin/env python3
"""memory_zero_hit.py — 增益验证门：找出"已采纳但从未被召回"的零增益 L1 条目。

EDGE（arXiv:2608.21946）启发：不是所有经验都值得长期保留，只有被验证带来
正增益（被召回/被用到）的才值得留在 L1。本工具不拦截写入，只做观测：
扫描 MEMORY.md/USER.md 条目，对照 .usage.json 的召回统计，报告零命中/陈旧条目。

用法：python3 memory_zero_hit.py [--days 30] [--json]
"""
import argparse
import json
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

HOME = Path.home()
MEMORIES = HOME / ".dsh" / "memories"
USAGE_FILE = MEMORIES / ".usage.json"
L1_FILES = [("MEMORY.md", MEMORIES / "MEMORY.md"), ("USER.md", MEMORIES / "USER.md")]


def parse_l1_entries(path: Path):
    """把 L1 文件按 § 拆成条目，返回 [{file, head40, text, tags}]"""
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    entries = []
    for chunk in text.split("\n§\n"):
        chunk = chunk.strip()
        if not chunk:
            continue
        # head40 = 条目原文前 40 字符（含标签前缀，与 rrf_recall._record_usage 的 key 对应）
        # usage key 形如 'MEMORY.md:#工作流 #验证 当 Obsidian...'（前 40 字符）
        head = chunk[:40]
        tags = re.findall(r"#([^\s#]+)", chunk)
        entries.append({"file": path.name, "head40": head, "text": chunk, "tags": tags})
    return entries


def load_usage():
    if not USAGE_FILE.exists():
        return {}
    try:
        return json.loads(USAGE_FILE.read_text(encoding="utf-8")).get("entries", {})
    except Exception:
        return {}


def entry_usage_key(fname: str, head40: str) -> str:
    return f"{fname}:{head40}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30, help="陈旧阈值（默认 30 天未召回）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--top", type=int, default=0, help="同时输出高复用 TOP N（默认 0=不输出）")
    args = ap.parse_args()
    now = datetime.now(timezone.utc)
    threshold = now - timedelta(days=args.days)

    usage = load_usage()
    report = []
    all_entries = []  # 全部条目（含 uses），供 TOP N
    for fname, path in L1_FILES:
        for ent in parse_l1_entries(path):
            key = entry_usage_key(ent["file"], ent["head40"])
            u = usage.get(key)
            uses = u.get("uses", 0) if u else 0
            last_use = u.get("last_use") if u else None
            last_dt = None
            if last_use:
                try:
                    last_dt = datetime.fromisoformat(last_use.replace("Z", "+00:00"))
                except Exception:
                    last_dt = None
            is_zero = uses == 0
            is_stale = last_dt is not None and last_dt < threshold
            never_used = last_dt is None  # 从未被召回过
            rec = {
                "file": fname,
                "tags": ent["tags"],
                "head40": ent["head40"],
                "uses": uses,
                "last_use": last_use,
                "status": "零命中（从未召回）" if never_used else ("陈旧（%d 天未召回）" % args.days if is_stale else "低命中"),
            }
            all_entries.append(rec)
            if is_zero or is_stale or never_used:
                report.append(rec)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    print(f"# memory_zero_hit 增益验证报告（阈值 {args.days} 天）")
    print(f"# 共 {len(report)} 条零命中/陈旧 L1 条目")
    if not report:
        print("\n✅ 所有 L1 条目均有召回记录或近期活跃，无零增益条目。")
    else:
        print()
        for i, r in enumerate(report, 1):
            print(f"{i}. [{r['status']}] {r['file']} 标签:{'/'.join(r['tags'])}")
            print(f"   内容: {r['head40']}…")
            print(f"   uses={r['uses']}  last_use={r['last_use']}")
            print()

    # TOP N 高复用（合并自 memory_hit_audit.py，2026-08-25）
    if args.top > 0:
        top = sorted(all_entries, key=lambda x: -x["uses"])[: args.top]
        print(f"## 高复用 TOP {len(top)}（uses 降序）")
        for i, r in enumerate(top, 1):
            print(f"{i}. ({r['uses']} 次) [{r['file']}] {r['head40']}…")
        print()


if __name__ == "__main__":
    main()
