#!/usr/bin/env python3
"""honcho_sync.py — dsh 会话 → Honcho L3 写入钩子（任务 A：Deriver 落地）

背景：Honcho（L3 语义层）在 hermes→dsh 迁移后无写入方，search 只剩 8/16 前旧数据。
本脚本从 dsh 会话 JSONL 提取有信息量的 user 消息 → 批量写入 Honcho conclusions
（observer=dsh-agent, observed=laozheng），供 honcho_search/chat 召回。

用法：
  honcho_sync.py sync [--days N]   增量同步最近 N 天 user 消息（默认 2）
  honcho_sync.py dry-run [--days N] 只打印将写入的消息（不写）
  honcho_sync.py check              显示 Honcho 中 dsh 结论数量

幂等：state 文件记录已处理 (session, seq)，重复运行不重写。
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import harvest  # 复用 unzstd / iter_session_files / extract_text_blocks

HONCHO_BASE = "http://127.0.0.1:8000"
# 2026-10-14：写入从 workspace=dsh 迁到 workspace=hermes。
# 原因：桥（L3 唯一读取入口）的口径是 hermes 工作区的 hermes→laozheng，写进 dsh 工作区的内容
# L3 永远看不到。现写 hermes/dsh-agent→laozheng，桥侧以 HONCHO_EXTRA_OBSERVERS=dsh-agent 并查，
# 既保住来源标识（observer=dsh-agent，与 dsh 工作区 370 条历史同身份）又能被召回。
WS = "life"
OBSERVER = "dsh-agent"
OBSERVED = "laozheng"
STATE_FILE = Path.home() / ".dsh" / "memories" / "honcho-sync-state.json"
MIN_LEN = 15  # 过滤超短指令/单字回复
MAX_MSG_PER_RUN = 50  # 单次上限，防止灌爆

import urllib.request


def _req(method, path, body=None, timeout=60):
    url = HONCHO_BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"processed": {}}


def save_state(st: dict) -> None:
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=1))


def collect_user_messages(days: int) -> list:
    """最近 days 天内有 mtime 的 session，提取 user 文本消息（含 seq 标识）。"""
    now = time.time()
    cutoff = now - days * 86400
    msgs = []
    for ws_name, sess_name, log in harvest.iter_session_files():
        if log.stat().st_mtime < cutoff:
            continue
        text = harvest.unzstd(log)
        if not text:
            continue
        for i, line in enumerate(text.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            seq = ev.get("seq", 0)
            if ev.get("type") != "user/message":
                continue
            data = ev.get("data") or {}
            src = data.get("source") or {}
            if src.get("kind") != "user":
                continue  # 只收真实用户消息，跳过插件/系统注入
            content = harvest.extract_text_blocks(data.get("content"))
            if not content or len(content) < MIN_LEN:
                continue
            # 跳过明显的纯工具结果回显（含长代码块/路径列表）
            if content.startswith(("```", "Error", "Traceback")):
                continue
            msgs.append({"key": f"{ws_name}/{sess_name}:{seq}", "text": content})
    return msgs


def sync(days: int, dry: bool) -> int:
    st = load_state()
    processed = st.setdefault("processed", {})
    msgs = collect_user_messages(days)
    to_write = [m for m in msgs if m["key"] not in processed]
    to_write = to_write[:MAX_MSG_PER_RUN]
    print(f"扫描到 {len(msgs)} 条 user 消息，未处理 {len(to_write)} 条")
    if dry:
        for m in to_write[:10]:
            print(f"  [dry-run] {m['text'][:60]}")
        return 0
    if not to_write:
        return 0
    # 批量写入（每次 20 条）
    for i in range(0, len(to_write), 20):
        batch = to_write[i:i + 20]
        conclusions = [{
            "content": m["text"],
            "observer_id": OBSERVER,
            "observed_id": OBSERVED,
        } for m in batch]
        try:
            _req("POST", f"/v3/workspaces/{WS}/conclusions", {"conclusions": conclusions}, timeout=120)
        except Exception as e:
            print(f"写入批次 {i // 20} 失败: {e}")
            save_state(st)
            return -1
        for m in batch:
            processed[m["key"]] = datetime.now(timezone.utc).isoformat()
        print(f"批次 {i // 20 + 1} 写入 {len(batch)} 条")
        time.sleep(1)
    save_state(st)
    return len(to_write)


def check():
    """自检：统计 dsh 会话已写入 Honcho 的结论数。

    2026-10-14 修：原实现 GET /v3/workspaces/{ws}/conclusions?observer_id=... 撞 405——
    该路径只挂 POST（/conclusions/list 与 /conclusions/query）。改用 /conclusions/list，
    filters 传 peer 名，返回 {items,total,page,size,pages}。
    """
    try:
        r = _req("POST", f"/v3/workspaces/{WS}/conclusions/list",
                 {"filters": {"observer": OBSERVER, "observed": OBSERVED}, "size": 3}, timeout=30)
        total = r.get("total") if isinstance(r, dict) else len(r)
        print(f"Honcho workspace[{WS}] {OBSERVER}→{OBSERVED} 结论数: {total}")
        for it in (r.get("items", []) if isinstance(r, dict) else r)[:3]:
            print(f"  - {str(it.get('content', ''))[:60]}")
    except Exception as e:
        print(f"查询失败: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["sync", "dry-run", "check"])
    ap.add_argument("--days", type=int, default=2)
    args = ap.parse_args()
    if args.cmd == "sync":
        n = sync(args.days, dry=False)
        print(f"同步完成，写入 {n} 条")
    elif args.cmd == "dry-run":
        sync(args.days, dry=True)
    else:
        check()


if __name__ == "__main__":
    main()
