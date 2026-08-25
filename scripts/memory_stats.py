#!/usr/bin/env python3
"""memory_stats.py — 维护记忆系统元数据（index.json）与纠正信号（corrections.jsonl）

设计（dsh记忆插件设计方案 §11/§12）：index.json 记录预算占用率/同步状态/实体表版本，
corrections.jsonl 供 skill-distill 消费。本脚本由 memory-maintenance timer 每日调用。

用法：
  memory_stats.py index    更新 memories/index.json（预算占用率等元数据）
  memory_stats.py signals  运行 harvest.py signals 提取纠正信号
  memory_stats.py all      两者都跑
"""
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

MEM = Path.home() / ".dsh" / "memories"
INDEX = MEM / "index.json"
USAGE = MEM / ".usage.json"
CORRECTIONS = MEM / "corrections.jsonl"
SCRIPT_DIR = Path.home() / ".dsh" / "plugins" / "dsh-memory" / "scripts"
# 预算单一数据源：cordis.patch.yml 的 dsh-memory config（避免硬编码失配，2026-08-23 修 4400→5000）
PATCH_YML = Path.home() / ".dsh" / "profiles" / "web" / "cordis.patch.yml"
DEFAULT_BUDGETS = {"MEMORY.md": 5000, "USER.md": 3000}


def _read_budgets() -> dict:
    """从 cordis.patch.yml 读 dsh-memory 的预算配置；读不到用默认（与插件 index.js 默认对齐）"""
    budgets = dict(DEFAULT_BUDGETS)
    try:
        txt = PATCH_YML.read_text(encoding="utf-8")
        import re
        mm = re.search(r"budgetMemory:\s*(\d+)", txt)
        mu = re.search(r"budgetUser:\s*(\d+)", txt)
        if mm:
            budgets["MEMORY.md"] = int(mm.group(1))
        if mu:
            budgets["USER.md"] = int(mu.group(1))
    except Exception:
        pass
    return budgets


def budget_stats() -> dict:
    limits = _read_budgets()
    out = {}
    for f, limit in limits.items():
        p = MEM / f
        if p.exists():
            n = len(p.read_text(encoding="utf-8"))
            out[f] = {"chars": n, "budget": limit, "pct": round(n / limit * 100, 1)}
    return out


def usage_stats() -> dict:
    """.usage.json：条目使用统计（rrf_recall lane1 命中时写入）。"""
    try:
        u = json.loads(USAGE.read_text(encoding="utf-8"))
        return {"entries": len(u.get("entries", {})), "last_recall": u.get("updated_at")}
    except Exception:
        return {"entries": 0, "last_recall": None}


def trigger_coverage() -> dict:
    """L1 trigger 覆盖率（2026-08-19 起哨兵）：每条 § 条目必须带 <!-- trigger: -->。
    写入端由 dsh-memory index.js ensureTrigger 保障；此处只检查报告。"""
    out = {}
    for f in ["MEMORY.md", "USER.md"]:
        p = MEM / f
        if not p.exists():
            continue
        entries = [e.strip() for e in p.read_text(encoding="utf-8").split("§") if e.strip()]
        missing = [e[:40] for e in entries if "<!-- trigger:" not in e]
        out[f] = {
            "total": len(entries),
            "with_trigger": len(entries) - len(missing),
            "missing": missing,
        }
    return out


def cmd_index():
    stats = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "budget": budget_stats(),
        "usage": usage_stats(),
        "l1_entries": len((MEM / "MEMORY.md").read_text(encoding="utf-8").split("§")) - 1,
        "suggestions_queue": sum(1 for _ in (MEM / "SUGGESTIONS.jsonl").open(encoding="utf-8")),
        "wiki_index_exists": (MEM / "wiki_index.sqlite").exists(),
        "trigger_coverage": trigger_coverage(),
    }
    INDEX.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    cov = stats["trigger_coverage"]
    for f, c in cov.items():
        pct = round(c["with_trigger"] / c["total"] * 100, 1) if c["total"] else 100.0
        flag = "" if c["with_trigger"] == c["total"] else f" ⚠️ 缺失 {len(c['missing'])} 条: {c['missing']}"
        print(f"{f} trigger 覆盖率: {c['with_trigger']}/{c['total']} ({pct}%){flag}")
    print(f"index.json 已更新: {json.dumps({k: v for k, v in stats.items() if k != 'trigger_coverage'}, ensure_ascii=False)}")


def cmd_signals():
    r = subprocess.run([sys.executable, str(SCRIPT_DIR / "harvest.py"), "signals"],
                       capture_output=True, text=True, timeout=120)
    print(r.stdout.strip() or r.stderr.strip())
    if CORRECTIONS.exists():
        n = sum(1 for _ in CORRECTIONS.open(encoding="utf-8"))
        print(f"corrections.jsonl 当前 {n} 条")
    else:
        print("corrections.jsonl 不存在（本轮无纠正信号）")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    if cmd in ("all", "index"):
        cmd_index()
    if cmd in ("all", "signals"):
        cmd_signals()


if __name__ == "__main__":
    main()
