#!/usr/bin/env python3
"""layer_audit.py — L1 四库分层审计（2026-10-17）

用途：memory_health 的常驻层体检。只看事实，不下判断：
  ① 常驻层体积（SOUL / 全局 AGENTS / 工作区 AGENTS）——每轮必付 token
  ② 召回层体积（MEMORY / USER）+ 条目数
  ③ 跨文件重复：同一段 ≥20 字正文出现在两个落点 = 双份维护
  ④ 空条目（`§\\n§`）
  ⑤ .usage.json 陈旧键（条目已删、统计还在）

分层口径（与 memory_suggest 归属门一致）：
  SOUL=人格红线 / AGENTS=常驻纪律与系统事实 / USER=人物画像 / MEMORY=触发式 if-then 知识 / wiki=项目记录
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HOME = Path.home()
MEM = HOME / ".dsh" / "memories"

LAYERS = [
    ("SOUL", HOME / ".dsh" / "persona" / "SOUL.md", "常驻"),
    ("AGENTS(全局)", HOME / ".dsh" / "AGENTS.md", "常驻"),
    ("AGENTS(工作区)", Path.cwd() / "AGENTS.md", "常驻"),
    ("USER", MEM / "USER.md", "召回"),
    ("MEMORY", MEM / "MEMORY.md", "召回"),
]

SHINGLE = 16          # 判重窗口
MIN_REPORT = 20       # 只报连续 ≥20 字的重复段


def norm(text: str) -> str:
    """去掉空白与行首列表符号，避免排版差异掩盖重复"""
    text = re.sub(r"(?m)^\s*[-*>]\s*", "", text)
    return re.sub(r"\s+", "", text)


def load(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


def runs(a: str, b: str) -> list[str]:
    """a 中与 b 重复的连续片段（按 SHINGLE 窗口判定）"""
    hits = [a[i:i + SHINGLE] in b for i in range(max(0, len(a) - SHINGLE + 1))]
    out, start = [], None
    for i, ok in enumerate(hits + [False]):
        if ok and start is None:
            start = i
        elif not ok and start is not None:
            seg = a[start:i + SHINGLE - 1]
            if len(seg) >= MIN_REPORT:
                out.append(seg)
            start = None
    return out


def main() -> int:
    lines: list[str] = []
    texts: dict[str, str] = {}
    for name, path, mode in LAYERS:
        raw = load(path)
        if not raw:
            lines.append(f"{name}: 未找到（{path}）")
            continue
        texts[name] = norm(raw)
        note = ""
        if mode == "召回" and name in ("MEMORY", "USER"):
            n = len([s for s in raw.split("§") if s.strip()])
            note = f"，{n} 条"
        lines.append(f"{name}: {len(raw)} 字符{note}（{mode}）")

    # ③ 跨文件重复
    dups: list[str] = []
    names = list(texts)
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            for seg in runs(texts[names[i]], texts[names[j]])[:2]:
                dups.append(f"{names[i]} ↔ {names[j]}「{seg[:36]}…」")
    lines.append(f"跨文件重复: {len(dups)} 处" + ("（同一事实只该有一处正文）" if dups else " ✓"))
    for d in dups[:6]:
        lines.append(f"  - {d}")

    # ④ 空条目
    for fn in ("MEMORY.md", "USER.md"):
        t = load(MEM / fn)
        if t:
            empty = len(re.findall(r"§\s*\n\s*§", t))
            if empty:
                lines.append(f"{fn} 空条目: {empty} 处（`§` 连排）")

    # ⑤ usage 陈旧键
    try:
        usage = json.loads(load(MEM / ".usage.json") or "{}")
        live = {}
        for fn in ("MEMORY.md", "USER.md"):
            for ent in load(MEM / fn).split("§"):
                ent = ent.strip()
                if ent:
                    live[f"{fn}:{ent[:40]}"] = 1
        stale = [k for k in usage if k not in live]
        if stale:
            lines.append(f".usage.json 陈旧键: {len(stale)} 条（条目已删，统计未回收）")
    except (json.JSONDecodeError, OSError) as e:
        lines.append(f".usage.json 读取失败: {e}")

    print("[layer_audit]\n" + "\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
