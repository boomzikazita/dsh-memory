#!/usr/bin/env python3
"""harvest.py — dsh-memory 转录与信号提取

功能:
  harvest.py index       增量转录 DSH 会话日志（JSONL.zstd）→ memories/index.sqlite FTS5（L2 召回源）
  harvest.py daily       追加今日进展到 daily/YYYY-MM-DD.md（摘要式）
  harvest.py signals     从最近 user 消息提取纠正信号（不对/不是/错了/改一下…）→ corrections.jsonl（供 skill-distill）

状态: memories/harvest-state.json（每 session 已索引到的最大 seq）
去重: 按 (session_id, seq) 幂等；跳过已注入的召回内容（插件侧标记 source.kind 非 user）
"""
import sys
import json
import sqlite3
import pathlib
import subprocess
import datetime
import re

HOME = pathlib.Path.home()
MEMORIES = HOME / ".dsh" / "memories"
SESSIONS_ROOT = HOME / ".dsh" / "sessions"
L2_DB = MEMORIES / "index.sqlite"
STATE_FILE = MEMORIES / "harvest-state.json"
CORRECTIONS_FILE = MEMORIES / "corrections.jsonl"
DAILY_DIR = MEMORIES / "daily"

CORRECTION_PATTERNS = re.compile(
    r"(不对|不是这样|不是这个|错了|说错了|改一下|重来|重新来|错了！|应该是|不是要|我说的是|理解错了|你搞错了|不对吧)"
)


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1))


def unzstd(path) -> str | None:
    try:
        out = subprocess.run(["unzstd", "-c", str(path)], capture_output=True, text=True, timeout=30)
        return out.stdout if out.returncode == 0 else None
    except Exception:
        return None


def iter_session_files():
    if not SESSIONS_ROOT.exists():
        return
    for ws in SESSIONS_ROOT.iterdir():
        if not ws.is_dir():
            continue
        for sess in ws.iterdir():
            log = sess / "session.jsonl.zstd"
            if log.exists():
                yield ws.name, sess.name, log


def extract_text_blocks(content) -> str | None:
    """从 content 块列表提取纯文本（跳过 reasoning/tool 块）"""
    if isinstance(content, str):
        return content.strip() or None
    if isinstance(content, list):
        parts = [b.get("text") for b in content if isinstance(b, dict) and b.get("type") == "text" and b.get("text")]
        return " ".join(parts).strip() if parts else None
    return None


FTS5_SCHEMA = "CREATE VIRTUAL TABLE IF NOT EXISTS session_chunks USING fts5(text, session, seq UNINDEXED, tokenize='trigram')"
CHUNK_META_SCHEMA = """CREATE TABLE IF NOT EXISTS chunk_meta(
        rowid INTEGER PRIMARY KEY, origin TEXT, session_kind TEXT, observed_at TEXT)"""


def fts_tokenizer(con) -> str | None:
    """探针：确认 session_chunks 虚表分词器。

    FTS5 config 表不记录 tokenizer（只有 version），须读 sqlite_master 的
    CREATE VIRTUAL TABLE SQL 判断。非 trigram → 触发重建（hermes 式启动探针）。
    """
    try:
        row = con.execute("SELECT sql FROM sqlite_master WHERE name='session_chunks'").fetchone()
        if row and row[0] and "tokenize='trigram'" in row[0]:
            return "trigram"
    except Exception:
        pass
    return None


def rebuild_fts(con, state: dict) -> dict:
    """分词器不符（非 trigram）→ 重建：DROP + 新 schema + 清空 state 全量重转录。

    2026-08-19 借鉴 hermes：默认 unicode61 把连续中文当巨型 token（「茅台」匹配不到
    「贵州茅台酒」），trigram 以 3-字符子串建索引，中文二元检索实质恢复。
    """
    con.execute("DROP TABLE IF EXISTS session_chunks")
    con.execute(FTS5_SCHEMA)
    con.execute("DROP TABLE IF EXISTS chunk_meta")
    con.execute(CHUNK_META_SCHEMA)
    print("harvest: FTS5 分词器非 trigram，已重建虚表并重置转录状态（全量重转录）", flush=True)
    return {}


def index_once() -> int:
    MEMORIES.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(L2_DB)
    con.execute(FTS5_SCHEMA)
    # provenance：来源可证（origin class + session kind + observed_at），与正文分开存
    con.execute(CHUNK_META_SCHEMA)
    state = load_state()
    # FTS5 启动探针（借鉴 hermes load_fts5_cjk_extension + 读探针）：分词器不符即重建
    if fts_tokenizer(con) != "trigram":
        state = rebuild_fts(con, state)
    added = 0
    for ws, sess, log in iter_session_files():
        # 会话种类门控（2026-08-21 修正，借鉴 OpenClaw memory-architecture）：
        # 自动化/子代理会话的转录【照常进 L2 检索层】——任务产物可被检索（有用产物留门），
        # 但 session_kind 标记为 automation，提案/升权侧据此排除【自动】提升（防脚手架噪音）。
        # 产物升权只走显式通道：用户 memory_suggest 确认，或"跨会话被多次召回"的复用证据。
        session_kind = "automation" if sess.startswith("dsh-automation-session-") else "interactive"
        key = f"{ws}/{sess}"
        last_seq = state.get(key, 0)
        raw = unzstd(log)
        if raw is None:
            continue
        chunks = []
        metas = []
        for line in raw.splitlines():
            try:
                ev = json.loads(line)
            except Exception:
                continue
            seq = ev.get("seq", 0)
            if seq <= last_seq:
                continue
            t = ev.get("type")
            data = ev.get("data") or {}
            text = None
            origin = None
            if t == "user/message":
                # 召回回路防护：只索引真实用户消息（source.kind=user），跳过插件/系统注入
                src = data.get("source") or {}
                if src.get("kind") == "user":
                    text = extract_text_blocks(data.get("content"))
                    origin = "owner"  # 用户可信通道输入
            elif t == "assistant/message":
                msg = data.get("message") or {}
                text = extract_text_blocks(msg.get("content"))
                origin = "agent"  # agent 从 owner 内容推导
            if text and origin:
                chunks.append((text[:2000], key, seq, origin))
        if chunks:
            cur = con.cursor()
            cur.executemany("INSERT INTO session_chunks(text, session, seq) VALUES (?,?,?)",
                            [(c[0], c[1], c[2]) for c in chunks])
            # FTS5 虚拟表不维护 lastrowid，用 max(rowid) 回推本批行号
            cur.execute("SELECT max(rowid) FROM session_chunks")
            last_rowid = cur.fetchone()[0]
            for i, (_, _, seq, origin) in enumerate(chunks):
                cur.execute("INSERT INTO chunk_meta(rowid, origin, session_kind, observed_at) VALUES (?,?,?,?)",
                            (last_rowid - (len(chunks) - 1 - i), origin, session_kind,
                             datetime.datetime.now().isoformat()))
            state[key] = max(c[2] for c in chunks)
            added += len(chunks)
            # turn/end 钩子补 daily 追加（设计缺口修复）：转录即留痕，程序自动标时间戳
            daily_append(f"会话转录 {sess} +{len(chunks)} 条（L2 索引，{session_kind}）")
    con.commit()
    con.close()
    save_state(state)
    return added


def daily_append(summary: str) -> None:
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.date.today().isoformat()
    f = DAILY_DIR / f"{today}.md"
    stamp = datetime.datetime.now().strftime("%H:%M")
    with open(f, "a") as fh:
        fh.write(f"- [{stamp}] {summary}\n")
    print(f"harvest: daily 已追加 {f.name}")


def extract_signals(limit: int = 200) -> int:
    """扫描最近 user 消息中的纠正信号，回溯被纠正的 assistant 消息一并记录。

    2026-08-23 补断环：纠正信号此前只沉淀不消费（corrections.jsonl 写了没人读，
    且没关联"被纠正的内容"，读也匹配不到 L2 索引）。现回溯 seq 之前最近一条
    assistant 消息存为 corrected 字段，供 rrf_recall L2 召回对被纠正内容做
    ⚠ 标注（确定性标记、不裁决——agent 自行降信任）。
    幂等：按 (session, seq) 去重，避免 timer 重复跑造成重复条目。
    """
    if not SESSIONS_ROOT.exists():
        return 0
    # 幂等去重：读已有 (session, seq) 集合
    existing = set()
    if CORRECTIONS_FILE.exists():
        try:
            for line in CORRECTIONS_FILE.open(encoding="utf-8"):
                try:
                    r = json.loads(line)
                    existing.add((r.get("session"), r.get("seq")))
                except Exception:
                    continue
        except Exception:
            pass
    found = 0
    MEMORIES.mkdir(parents=True, exist_ok=True)
    for ws in SESSIONS_ROOT.iterdir():
        if not ws.is_dir():
            continue
        for sess in ws.iterdir():
            log = sess / "session.jsonl.zstd"
            if not log.exists():
                continue
            raw = unzstd(log)
            if raw is None:
                continue
            last_assistant = ""  # 每个 session 独立回溯：seq 之前最近一条 assistant 消息
            for line in raw.splitlines():
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                data = ev.get("data") or {}
                t = ev.get("type")
                if t == "assistant/message":
                    msg = data.get("message") or {}
                    atext = extract_text_blocks(msg.get("content"))
                    if atext:
                        last_assistant = atext
                    continue
                if t != "user/message":
                    continue
                src = data.get("source") or {}
                if src.get("kind") != "user":
                    continue  # 跳过插件/系统注入（召回回路防护）
                text = extract_text_blocks(data.get("content")) or ""
                if not CORRECTION_PATTERNS.search(text):
                    continue
                key = (sess.name, ev.get("seq"))
                if key in existing:
                    continue
                record = {
                    "ts": ev.get("ts") or datetime.datetime.now().isoformat(),
                    "session": sess.name,
                    "seq": ev.get("seq"),
                    "text": text.strip()[:500],
                }
                if last_assistant:
                    record["corrected"] = last_assistant[:500]
                with open(CORRECTIONS_FILE, "a") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                found += 1
                if found >= limit:
                    return found
    return found


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "index"
    if cmd == "index":
        n = index_once()
        print(f"harvest: 索引完成，新增 {n} 条会话块")
    elif cmd == "daily":
        daily_append(sys.argv[2] if len(sys.argv) > 2 else "（今日转录）")
    elif cmd == "signals":
        n = extract_signals()
        print(f"harvest: 提取纠正信号 {n} 条 → corrections.jsonl")
    else:
        raise SystemExit(f"harvest: 未知命令 {cmd}")


if __name__ == "__main__":
    main()
