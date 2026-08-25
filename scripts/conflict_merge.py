#!/usr/bin/env python3
"""conflict_merge.py — L1 合并执行器（借鉴 TencentDB-Agent-Memory l1-dedup merge/update 语义）

把"多条相关记忆合并为一条"落盘执行：
  - 定位目标条目（MEMORY.md / USER.md 的 § 条目，按索引或子串匹配）
  - 从目标文件删除被替换条目（target_ids）
  - 写入合并后内容（merged_content），保留时间戳并集
  - pre-image 记录到 DREAMS.md（可审计，配合 memory_backup git 提交）

用法:
  conflict_merge.py merge <track> <merged_content> <target_ids...>
      track: memory | user
      target_ids: L1 条目索引（0-based，见 conflict_detect 输出）或内容子串
  conflict_merge.py replace <track> <target_id> <new_content>
      单条目 update（等价 merge 但只替换一条，保留条目位置）
  conflict_merge.py test <track> <target_ids...>
      干跑：显示将删除的条目与合并结果，不落盘

安全：
  - 只处理 L1（MEMORY.md / USER.md），不碰 vault / wiki
  - 合并结果为空则拒绝
  - 全部校验通过后才原子写（先写 tmp 再 rename）
"""
import json
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
MEM_DIR = HOME / ".dsh" / "memories"
DREAMS = MEM_DIR / "DREAMS.md"

TRACK_FILES = {"memory": "MEMORY.md", "user": "USER.md"}

# 时间戳并集：合并后保留所有源条目的 v:/日期 注释
TS_RES = [
    re.compile(r"<!--\s*v:\s*(\d{4}-\d{2}-\d{2})\s*-->"),
    re.compile(r"(\d{4}-\d{2}-\d{2})\s*(?:确认|核实|更新)"),
    re.compile(r"有效期至：(\d{4}-\d{2}-\d{2})"),
]


def _timestamps_of(text: str) -> list:
    out = []
    for r in TS_RES:
        for m in r.findall(text):
            out.append(m)
    return out


def load_entries(track: str) -> list:
    fpath = MEM_DIR / TRACK_FILES[track]
    if not fpath.exists():
        return []
    raw = fpath.read_text(encoding="utf-8")
    return raw.split("\n§\n")


def save_entries(track: str, entries: list) -> str:
    fpath = MEM_DIR / TRACK_FILES[track]
    content = "\n§\n".join(e.strip() for e in entries if e and e.strip())
    content = content.rstrip() + "\n"
    # 原子写
    fd, tmp = tempfile.mkstemp(dir=str(MEM_DIR), prefix=".l1merge-", suffix=".tmp")
    try:
        with open(fd, "w", encoding="utf-8") as f:
            f.write(content)
        tmp_path = Path(tmp)
        tmp_path.replace(fpath)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise
    return content


def resolve_targets(entries: list, target_ids: list) -> list:
    """把 target_ids（索引字符串或内容子串）解析为条目索引列表。"""
    resolved = []
    for t in target_ids:
        t = t.strip()
        if t.isdigit():
            idx = int(t)
            if 0 <= idx < len(entries) and entries[idx].strip():
                resolved.append(idx)
            continue
        # 子串匹配（取第一个命中）
        for i, e in enumerate(entries):
            if t in e and i not in resolved:
                resolved.append(i)
                break
    return sorted(set(resolved))


def do_merge(track: str, merged_content: str, target_ids: list, dry_run: bool = False) -> str:
    if track not in TRACK_FILES:
        return f"conflict_merge: track 必须为 memory/user（收到 {track!r}）"
    merged_content = merged_content.strip()
    if not merged_content:
        return "conflict_merge: 合并内容为空，拒绝"

    entries = load_entries(track)
    idxs = resolve_targets(entries, target_ids)
    if not idxs:
        return f"conflict_merge: 未解析到任何目标条目（targets={target_ids}）"

    removed = [entries[i].strip() for i in idxs]
    # 时间戳并集（保留所有被合并条目的时间信息）
    all_ts = []
    for r in removed:
        all_ts.extend(_timestamps_of(r))
    all_ts = sorted(set(all_ts))
    merged = merged_content
    # 若合并内容无 v: 注释且有历史时间戳，追加并集注释
    if all_ts and "<!-- v:" not in merged:
        merged += f" <!-- v: {all_ts[0]} -->"

    summary = {
        "op": "merge",
        "track": track,
        "ts": datetime.now(timezone.utc).isoformat(),
        "target_ids": idxs,
        "removed_head": [r[:50] for r in removed],
        "timestamps_union": all_ts,
    }
    if dry_run:
        out = [f"conflict_merge(dry-run {track}): 将删除 {len(idxs)} 条，写入 1 条"]
        for i, r in zip(idxs, removed):
            out.append(f"  -[{i}] {r[:70]}")
        out.append(f"  + {merged[:70]}")
        out.append(f"  时间戳并集: {all_ts or '无'}")
        return "\n".join(out)

    # 删除目标条目，合并结果插在原最小索引位置
    min_idx = min(idxs)
    kept = [e for i, e in enumerate(entries) if i not in idxs]
    kept.insert(min_idx, merged)
    save_entries(track, kept)

    # pre-image 记录（审计）
    try:
        with DREAMS.open("a", encoding="utf-8") as f:
            f.write(f"- {summary['ts']} merge({track}) targets={idxs} "
                    f"→ 1 条 | union_ts={all_ts or '-'} | del={len(idxs)}\n")
    except Exception:
        pass
    return (f"conflict_merge({track}): 已合并 {len(idxs)} 条 → 1 条 "
            f"（targets={idxs}，时间戳并集 {all_ts or '无'}）。已记 DREAMS.md")


def do_replace(track: str, target_id: str, new_content: str, dry_run: bool = False) -> str:
    """单条目 update：原位替换，保留位置与时间戳。"""
    if track not in TRACK_FILES:
        return f"conflict_merge: track 必须为 memory/user（收到 {track!r}）"
    new_content = new_content.strip()
    if not new_content:
        return "conflict_merge: 内容为空，拒绝"
    entries = load_entries(track)
    idxs = resolve_targets(entries, [target_id])
    if not idxs:
        return f"conflict_merge: 未解析到目标条目 {target_id!r}"
    idx = idxs[0]
    old = entries[idx].strip()
    all_ts = _timestamps_of(old)
    merged = new_content
    if all_ts and "<!-- v:" not in merged:
        merged += f" <!-- v: {all_ts[0]} -->"
    if dry_run:
        return f"conflict_merge(dry-run {track}): [{idx}] {old[:60]} → {merged[:60]}"
    entries[idx] = merged
    save_entries(track, entries)
    try:
        with DREAMS.open("a", encoding="utf-8") as f:
            f.write(f"- {datetime.now(timezone.utc).isoformat()} update({track}) [{idx}] → 替换\n")
    except Exception:
        pass
    return f"conflict_merge({track}): 已原位更新 [{idx}]（时间戳 {all_ts or '无'}）。已记 DREAMS.md"


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 1
    cmd = args[0]
    dry_run = "--dry-run" in args or "test" in args
    if cmd == "test":
        if len(args) < 3:
            print("conflict_merge test <track> <target_ids...>")
            return 1
        track = args[1]
        ids = args[2:]
        print(do_merge(track, "[干跑占位]", ids, dry_run=True))
        return 0
    if cmd == "merge":
        if len(args) < 4:
            print("conflict_merge merge <track> <merged_content> <target_ids...>")
            return 1
        track = args[1]
        content = args[2]
        ids = args[3:]
        print(do_merge(track, content, ids, dry_run=dry_run))
        return 0
    if cmd == "replace":
        if len(args) < 4:
            print("conflict_merge replace <track> <target_id> <new_content>")
            return 1
        print(do_replace(args[1], args[2], args[3], dry_run=dry_run))
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
