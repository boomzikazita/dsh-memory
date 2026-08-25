#!/usr/bin/env python3
"""conflict_detect.py — L1 批量冲突检测（借鉴 TencentDB-Agent-Memory l1-dedup 批量语义）

腾讯版：LLM 批量比较多条【新记忆】与【候选池】，输出 store/skip/update/merge 四动作
+ 多对多 target_ids + 时间戳并集 + priority 提升。本机是 agent 驱动环境（语义判断
由 agent 完成），本脚本做**机械检测层**：批量扫描 pending 提案 与 既有 L1 条目，
产出冲突候选对 + 建议动作，供 agent 决策后走 memory_merge 执行。

用法:
  conflict_detect.py                    # 全量：pending 提案 vs L1 + 提案 vs 提案
  conflict_detect.py --track memory     # 只看 MEMORY.md
  conflict_detect.py --track user       # 只看 USER.md
  conflict_detect.py --json             # 机器可读输出（agent 解析）

动作判定（机械版，语义兜底由 agent 复核）:
  - skip:  与既有条目重叠 ≥0.6 且新内容无增量（长度比 <1.2）
  - update: 重叠 ≥0.6 且新内容更长/更新（长度比 ≥1.2）
  - merge: 重叠 0.35-0.6，或标签交集 ≥1（跨条目互补，建议合并）
  - store: 无重叠候选（正常新增）

输出: 每组候选含 {target_kind, target_id/索引, head, overlap, action, reason}
"""
import json
import sys
import re
from pathlib import Path

HOME = Path.home()
MEM_DIR = HOME / ".dsh" / "memories"
SUGGESTIONS = MEM_DIR / "SUGGESTIONS.jsonl"

# 与 dsh-memory index.js findOverlaps 相同的 bigram 度量（保持判定一致）
def bigrams_of(text: str) -> set:
    s = re.sub(r"#[\u4e00-\u9fffA-Za-z0-9_-]+", " ", text)
    s = re.sub(r"<!--.*?-->", " ", s)
    grams = set()
    for i in range(len(s) - 1):
        g = s[i:i + 2]
        if re.search(r"[\u4e00-\u9fff]", g) or (g[0].isalnum() and g[1].isalnum()):
            grams.add(g)
    return grams


def overlap_of(a: str, b: str) -> float:
    ga, gb = bigrams_of(a), bigrams_of(b)
    if not ga or not gb:
        return 0.0
    hit = sum(1 for g in ga if g in gb)
    return hit / min(len(ga), len(gb))


def tags_of(text: str) -> set:
    return set(re.findall(r"#([\u4e00-\u9fffA-Za-z0-9_-]+)", text))


def load_l1(track: str) -> list:
    """返回 [(index, text), ...]（§ 分隔，跳过 <10 字符空条目）"""
    fname = "USER.md" if track == "user" else "MEMORY.md"
    fpath = MEM_DIR / fname
    if not fpath.exists():
        return []
    out = []
    for i, ent in enumerate(fpath.read_text(encoding="utf-8").split("\n§\n")):
        e = ent.strip()
        if len(e) >= 10:
            out.append((i, e))
    return out


def load_pending() -> list:
    """返回 [suggest_item, ...]（status=pending）"""
    if not SUGGESTIONS.exists():
        return []
    out = []
    for line in SUGGESTIONS.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except Exception:
            continue
        if item.get("status") == "pending":
            out.append(item)
    return out


def decide_action(new_text: str, target_text: str, ov: float) -> tuple:
    """机械动作判定：返回 (action, reason)。语义兜底由 agent 复核。"""
    new_len = len(new_text)
    tgt_len = len(target_text)
    ratio = new_len / tgt_len if tgt_len else 99.0
    tag_hit = len(tags_of(new_text) & tags_of(target_text))
    if ov >= 0.6:
        if ratio < 1.2:
            return "skip", f"重叠 {ov:.0%} 且新内容无增量（长比 {ratio:.1f}）"
        return "update", f"重叠 {ov:.0%} 且新内容更完整（长比 {ratio:.1f}）"
    if ov >= 0.35 or (ov >= 0.2 and tag_hit >= 1):
        return "merge", f"重叠 {ov:.0%}，标签交集 {tag_hit}，建议合并互补信息"
    return "store", "无重叠候选，正常新增"


def scan(track: str, as_json: bool = False) -> str:
    pend = load_pending()
    pend_track = [p for p in pend if p.get("track") in (track, None) or p.get("track") == track]
    l1 = load_l1(track)
    groups = []  # 每组: {new, new_id, track, candidates: [...]}
    seen_new = set()

    def candidates_for(new_text: str) -> list:
        cands = []
        # ① 与 L1 既有条目
        for idx, text in l1:
            ov = overlap_of(new_text, text)
            if ov < 0.2:
                continue
            action, reason = decide_action(new_text, text, ov)
            cands.append({
                "kind": f"L1:{track}",
                "target_id": str(idx),
                "head": text[:60],
                "overlap": round(ov * 100),
                "tags": len(tags_of(new_text) & tags_of(text)),
                "action": action,
                "reason": reason,
            })
        # ② 与 pending 提案（同 track，排除自己）
        for p in pend_track:
            if p.get("id") in seen_new:
                continue
            ov = overlap_of(new_text, p.get("content", ""))
            if ov < 0.2:
                continue
            action, reason = decide_action(new_text, p.get("content", ""), ov)
            cands.append({
                "kind": "pending",
                "target_id": p.get("id", ""),
                "head": (p.get("content") or "")[:60],
                "overlap": round(ov * 100),
                "tags": len(tags_of(new_text) & tags_of(p.get("content", ""))),
                "action": action,
                "reason": reason,
            })
        cands.sort(key=lambda c: (-c["overlap"], -c["tags"]))
        return cands[:4]

    # 只扫 pending 提案（新内容来源）；L1↔L1 归 weekly-review 管
    for p in pend_track:
        content = p.get("content", "")
        if not content or p.get("id") in seen_new:
            continue
        seen_new.add(p.get("id"))
        cands = candidates_for(content)
        if cands:
            groups.append({
                "new_id": p.get("id"),
                "new_track": p.get("track"),
                "new_head": content[:60],
                "candidates": cands,
            })

    if as_json:
        return json.dumps({"groups": groups, "pending_total": len(pend), "l1_total": len(l1)}, ensure_ascii=False, indent=2)

    if not groups:
        return f"conflict_detect({track}): 无冲突候选（pending {len(pend)} / L1 {len(l1)}）"
    lines = [f"conflict_detect({track}): {len(groups)} 组冲突候选（pending {len(pend)} / L1 {len(l1)}）"]
    for g in groups:
        lines.append(f"  ⚠️ 提案 {g['new_id']} ({g['new_track']})「{g['new_head']}…」")
        for c in g["candidates"]:
            lines.append(f"    → {c['action']:6s} {c['kind']}[{c['overlap']}%{',标签'+str(c['tags']) if c['tags'] else ''}] {c['target_id']} 「{c['head']}…」 {c['reason']}")
    return "\n".join(lines)


def main() -> int:
    args = sys.argv[1:]
    track = "memory"
    as_json = False
    if "--track" in args:
        track = args[args.index("--track") + 1]
    if "--json" in args:
        as_json = True
    print(scan(track, as_json))
    return 0


if __name__ == "__main__":
    sys.exit(main())
