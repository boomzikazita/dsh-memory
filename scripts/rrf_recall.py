#!/usr/bin/env python3
"""rrf_recall.py — dsh-memory 五路 RRF 召回（L1 关键词 / L2 会话 / L3 语义(可选) / L4 wiki 活指针 / L5 时间窗）

用法:
  rrf_recall.py "<query>" [--budget 800] [--topk 5] [--l3 file.json] [--date 2026-08-17] [--json]

召回源:
  L1  MEMORY.md / USER.md 条目（§ 分隔），实体/标签/关键词 in 匹配（中文无需 \b）
  L2  memories/index.sqlite FTS5（harvest.py 增量转录 DSH 会话日志建立；未建则降级为空）
  L3  可选：--l3 传入 Honcho/MCP 召回结果 JSON [{text, score}]
  L4  MEMORY 中 `→ wiki/相对路径` 活指针，命中实体时跟随读取目标文件（≤400 字符摘要）
  L5  时间窗：--date 指定日期（YYYY-MM-DD / today / yesterday），扫描会话 JSONL 原文
      按消息 time 字段过滤该日 user/assistant 消息（一手材料，溯源用）

融合: RRF score = Σ 1/(k+rank)，k=60，top-k 默认 5
输出: 证据账本 [来源 · 可信度 · 文本]；--json 输出结构化
"""
import os
import sys
import re
import json
import hashlib
import sqlite3
import argparse
import pathlib
import datetime
import subprocess
from collections import defaultdict

HOME = pathlib.Path.home()
MEMORIES = HOME / ".dsh" / "memories"
SESSIONS_ROOT = HOME / ".dsh" / "sessions"
L1_FILES = [MEMORIES / "MEMORY.md", MEMORIES / "USER.md"]
L2_DB = MEMORIES / "index.sqlite"
HITS_DB = MEMORIES / "hits.sqlite"
CORRECTIONS_FILE = MEMORIES / "corrections.jsonl"
WIKI_ROOT = HOME / "Nutstore Files" / "我的坚果云" / "wiki" / "raw"
K = 60

# 实体表（沿用 hermes rrf_fuser.py，中文用 in 操作符；新增人物/领域词按维护规格补充）
_KNOWN_PERSONS = frozenset({"老郑", "郑鑫燚", "陈丹", "句号", "藏妤", "刘兰", "何启敏", "sophia", "张盈盈"})
_KNOWN_PROJECTS = frozenset({"HMS", "Shadow-Weave", "RRF", "Hermes", "Mem0", "Letta", "Memory-OS", "DSH", "OpenClaw"})
_KNOWN_TOPICS = frozenset({"A股", "情感", "控制论", "会议纪要", "Vaultwarden", "Honcho", "零信任", "RDP",
                           "ZeroTier", "飞书", "公文", "SiYuan", "Obsidian", "graphify", "skill", "cron"})

_ALL_ENTITIES = sorted(
    [(e, "person") for e in _KNOWN_PERSONS]
    + [(e, "project") for e in _KNOWN_PROJECTS]
    + [(e, "topic") for e in _KNOWN_TOPICS],
    key=lambda x: len(x[0]), reverse=True,
)

_STOP = frozenset({"的", "了", "吗", "呢", "什么", "怎么", "这个", "那个", "一下", "一个", "是", "在", "和", "与"})


def extract_entities(text: str) -> list:
    hits = []
    for name, etype in _ALL_ENTITIES:
        if name in text:
            hits.append((name, etype))
    return hits


def split_entries(text: str) -> list:
    return [e.strip() for e in text.split("§") if e.strip()]


def load_l1() -> list:
    """L1 条目: {file, text, tags, entities}"""
    out = []
    for f in L1_FILES:
        if not f.exists():
            continue
        for ent in split_entries(f.read_text()):
            tags = re.findall(r"#(\S+)", ent)
            out.append({"file": f.name, "text": ent, "tags": tags, "entities": [e for e, _ in extract_entities(ent)]})
    return out


def bigrams(s: str) -> set:
    return {s[i:i + 2] for i in range(len(s) - 1)}


def lane1(query: str, q_entities: set) -> list:
    """L1 关键词匹配：实体命中权重高，字符 bigram 重叠兜底（中文无需 \b）。

    2026-08-22 加入时间衰减：score × decay_factor（常驻标签不衰减；
    近期用过/新条目权重高，久未用/旧条目降权）。仅影响排序，不删条目。
    """
    qb = bigrams(query)
    scored = []
    hits = []
    usage = _load_usage()
    for ent in load_l1():
        score = 0.0
        ent_set = set(ent["entities"])
        # 实体命中：查询实体 ∩ 条目实体
        common = q_entities & ent_set
        score += 3.0 * len(common)
        # 字符 bigram 重叠（中文关键词匹配的核心）
        inter = qb & bigrams(ent["text"])
        score += 0.5 * len(inter)
        # 标签命中：条目标签是查询子串
        for tag in ent["tags"]:
            if tag in query:
                score += 2.0
        if score >= 1.0:
            decay = decay_factor(ent["text"], ent["tags"], ent["file"], usage)
            scored.append((round(score * decay, 2), f"[{ent['file']}] {ent['text']}"))
            hits.append((ent["file"], ent["text"][:40]))
    scored.sort(key=lambda x: -x[0])
    _record_usage(hits)
    return scored[:20]


def _record_usage(hits: list) -> None:
    """记录 L1 条目使用统计（.usage.json，供衰减机制判断 30 天未用条目）"""
    try:
        import json
        from datetime import datetime, timezone
        p = HOME / ".dsh" / "memories" / ".usage.json"
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            data = {"entries": {}}
        if not isinstance(data, dict) or "entries" not in data:
            data = {"entries": {}}
        entries = data.setdefault("entries", {})
        now = datetime.now(timezone.utc).isoformat()
        for fname, head in hits:
            key = f"{fname}:{head}"
            e = entries.setdefault(key, {"uses": 0, "last_use": None})
            e["uses"] += 1
            e["last_use"] = now
        data["updated_at"] = now
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass  # usage 统计失败不影响召回主链路


# ---------- 时间衰减（借鉴论文 Rethinking Memory 4.1.5 forgetting & retention） ----------
# 常驻标签：规则/安全/边界/身份/红线类条目永不衰减（稳定性优先）
_PERSISTENT_TAGS = frozenset({"安全", "规则", "边界", "红线", "身份", "硬约束", "常驻"})
# 衰减档位：(年龄上限天, 系数)。30 天内 1.0 → 90 天 0.7 → 180 天 0.5 → 更久 0.3
_DECAY_TIERS = ((30, 1.0), (90, 0.7), (180, 0.5), (float("inf"), 0.3))


def _load_usage() -> dict:
    """读取 .usage.json（L1 条目 last_use/uses 统计）"""
    try:
        p = MEMORIES / ".usage.json"
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("entries"), dict):
                return data["entries"]
    except Exception:
        pass
    return {}


def _entry_version_date(text: str):
    """L1 条目 <!-- v: YYYY-MM-DD --> 版本时间戳（无 usage 时的衰减 fallback）"""
    m = re.search(r"<!--\s*v:\s*(\d{4}-\d{2}-\d{2})\s*-->", text)
    if m:
        try:
            return datetime.date.fromisoformat(m.group(1))
        except ValueError:
            return None
    return None


def decay_factor(text: str, tags: list, fname: str, usage: dict | None = None) -> float:
    """时间衰减系数（0.3~1.0）：
    - 常驻标签（#安全/#规则/#边界/#红线/#身份/#硬约束/#常驻）→ 1.0 永不衰减
    - 衰减基准 = 条目 <!-- v: --> 版本时间戳（知识确立/最后更新的老化信号）
    - 无 v: → 文件 mtime（兜底）；无时间信息 → 1.0 不衰减（宁留不误伤）
    - ⚠️ 不用 usage.last_use 作基准：命中即刷新会掩盖老化，导致衰减永不生效
      （2026-08-22 实测：一次召回把所有条目 last_use 刷成当天，全 1.0）
    """
    if any(t in _PERSISTENT_TAGS for t in tags):
        return 1.0
    ref = _entry_version_date(text)
    if ref is None:
        # 无 v: 用文件 mtime（L1 文件整体改动时间，粗粒度但可用）
        try:
            mt = datetime.date.fromtimestamp((MEMORIES / fname).stat().st_mtime)
        except Exception:
            return 1.0
        ref = mt
    age = (datetime.date.today() - ref).days
    for limit, coef in _DECAY_TIERS:
        if age <= limit:
            return coef
    return 0.3


def lane2(query: str) -> list:
    """L2 会话 FTS5（index.sqlite 由 harvest.py 建立；未建降级空）

    trigram 分词器适配（2026-08-19 迁移，借鉴 hermes 中文检索方案）：
      - ≥3 字符 token 走 FTS MATCH（双引号包裹，规避 FTS5 语法特殊字符）
      - <3 字符 token（如「A股」）trigram 不建索引 → LIKE 降级扫描
    """
    if not L2_DB.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{L2_DB}?mode=ro", uri=True)
        cur = con.cursor()
        tokens = [t for t in re.split(r"[\s,，。;；:：!！?？()（）\[\]【】{}<>《》\"'“”‘’]+", query) if t]
        def _clean(t): return re.sub(r'["*():^~\-]', '', t)  # FTS5 特殊字符清洗
        long_tokens = [t for t in (_clean(t) for t in tokens) if len(t) >= 3]
        rows = []
        if long_tokens:
            fts_q = " AND ".join(f'"{t}"' for t in long_tokens)
            try:
                cur.execute("SELECT text FROM session_chunks WHERE session_chunks MATCH ? LIMIT 20", (fts_q,))
                rows = [r[0] for r in cur.fetchall()]
            except Exception:
                rows = []
        # 短词降级：trigram 最小索引单元 3 字符，2 字符查询（A股/飞书/文档）走 LIKE
        short_tokens = [t for t in tokens if 1 <= len(t) < 3]
        for st in short_tokens:
            try:
                cur.execute("SELECT text FROM session_chunks WHERE text LIKE ? LIMIT 10", (f"%{st}%",))
                for (r,) in cur.fetchall():
                    if r not in rows:
                        rows.append(r)
            except Exception:
                pass
        con.close()
        return rows[:20]
    except Exception:
        return []


def load_corrected() -> list:
    """加载 corrections.jsonl 中被纠正的 assistant 消息（corrected 字段）。

    2026-08-23 补断环：纠正信号此前只写不读。此函数供 L2 召回对被纠正内容做
    ⚠ 标注（确定性标记、不裁决）。旧条目无 corrected 字段 → 返回空列表（自然跳过）。
    """
    out = []
    if not CORRECTIONS_FILE.exists():
        return out
    try:
        for line in CORRECTIONS_FILE.open(encoding="utf-8"):
            try:
                r = json.loads(line)
                c = r.get("corrected")
                if c:
                    out.append(c)
            except Exception:
                continue
    except Exception:
        pass
    return out


def is_corrected(text: str, corrected_list: list) -> bool:
    """判断 L2 召回文本是否命中"被纠正过的内容"。

    匹配：① 前缀匹配（同一 assistant 消息在 index 存 text[:2000]、corrected 存
    text[:500]，两者同源截断、前缀一致）；② bigram 重叠兜底（防标点/空白差异）。
    """
    if not corrected_list or not text:
        return False
    tb = bigrams(text)
    for c in corrected_list:
        if text.startswith(c) or c.startswith(text):
            return True
        cb = bigrams(c)
        if cb and len(tb & cb) / len(cb) > 0.6:
            return True
    return False


def lane3(l3_file: str | None) -> list:
    """L3 语义：可选，由插件传入 Honcho/MCP 结果 [{text, score}]"""
    if not l3_file:
        return []
    try:
        with open(l3_file) as f:
            data = json.load(f)
        return [(float(d.get("score", 0.5)), d["text"]) for d in data if d.get("text")]
    except Exception:
        return []


# ---------- L3 MCP 直连（honcho-codex-bridge，streamable-http） ----------
MCP_URL = os.environ.get("DSH_HONCHO_MCP_URL", "http://10.173.48.28:8001/mcp")
# Token 优先级：环境变量 → 密钥文件 → 占位符。
# 2026-10-14 新增文件回退：dsh web/headless 进程启动环境里没有 DSH_HONCHO_MCP_TOKEN，
# 原先直接用占位符 → 桥返回 401 → lane3 静默降级为 []（L3 实际长期失效）。
_TOKEN_FILE = pathlib.Path.home() / ".dsh" / ".honcho-mcp-token"


def _mcp_token() -> str:
    env = os.environ.get("DSH_HONCHO_MCP_TOKEN")
    if env:
        return env
    try:
        tok = _TOKEN_FILE.read_text().strip()
        if tok:
            return tok
    except Exception:
        pass
    return "CHANGEME_HONCHO_TOKEN"


MCP_TOKEN = _mcp_token()


def _mcp_rpc(method: str, params: dict, session_id: str | None = None):
    """streamable-http JSON-RPC；返回 (Mcp-Session-Id, SSE 原始文本)"""
    import urllib.request
    body = json.dumps({"jsonrpc": "2.0", "id": 99, "method": method, "params": params}).encode()
    req = urllib.request.Request(MCP_URL, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    req.add_header("Authorization", f"Bearer {MCP_TOKEN}")
    if session_id:
        req.add_header("Mcp-Session-Id", session_id)
    with urllib.request.urlopen(req, timeout=15) as resp:
        sid = resp.headers.get("Mcp-Session-Id")
        return sid, resp.read().decode()


def _mcp_sse_text(data: str) -> list:
    """解析 SSE data 行中的 result.content[].text"""
    out = []
    for line in data.splitlines():
        if line.startswith("data:"):
            try:
                d = json.loads(line[5:].strip())
            except Exception:
                continue
            for c in d.get("result", {}).get("content", []):
                if c.get("type") == "text" and c.get("text"):
                    out.append(c["text"])
    return out


def lane3_mcp(query: str) -> list:
    """L3 语义召回：经 MCP 调用 honcho_search；桥不可达则静默降级（L3 为可选层）"""
    try:
        sid, _init = _mcp_rpc("initialize", {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "dsh-memory", "version": "0.1.0"},
        })
        sid2, out = _mcp_rpc("tools/call", {"name": "honcho_search", "arguments": {"query": query}}, sid)
        texts = _mcp_sse_text(out)
        return [(1.0, t) for t in texts]
    except Exception as e:
        # 默认静默（L3 可选层）；排查时 DSH_MEMORY_L3_DEBUG=1 显示降级原因
        if os.environ.get("DSH_MEMORY_L3_DEBUG"):
            print(f"[lane3_mcp] 降级 → []: {type(e).__name__}: {e}", file=sys.stderr)
        return []


def _resolve_wiki(rel: str) -> list:
    """解析 wiki 指针：兼容 `wiki/raw/...`（相对 wiki 根）与 `raw/...`（相对 WIKI_ROOT）；支持 <名> 模板 glob"""
    cands = [WIKI_ROOT / rel, WIKI_ROOT.parent / rel]
    for c in cands:
        if c.exists():
            return [c]
    out = []
    for base in (WIKI_ROOT, WIKI_ROOT.parent):
        try:
            out.extend(sorted(base.glob(rel.replace('<名>', '*'))))
        except Exception:
            pass
    return out[:3]


def _title_to_rel(title: str) -> str | None:
    """wiki 标题 → rel_path（相对 WIKI_ROOT 的 posix 路径）。供 BFS 邻接节点显示。"""
    if not WIKI_DB.exists():
        return None
    try:
        import sqlite3
        con = sqlite3.connect(WIKI_DB)
        row = con.execute("SELECT rel_path FROM wiki_docs WHERE title=?", (title,)).fetchone()
        return row[0] if row else None
    except Exception:
        return None


def _session_log(sess_dir):
    """定位会话日志（2026-10-14 修：dsh 0.2 起为 session.v4.jsonl.zstd；旧名 session.jsonl.zstd）"""
    for pat in ("session*.jsonl.zstd", "session*.jsonl"):
        hits = sorted(p for p in sess_dir.glob(pat) if p.is_file())
        if hits:
            return hits[-1]
    return None


def _unzstd(path) -> str | None:
    """解压会话日志 .zstd（复用 harvest.py 的 unzstd 实现）"""
    try:
        out = subprocess.run(["unzstd", "-c", str(path)], capture_output=True, text=True, timeout=30)
        return out.stdout if out.returncode == 0 else None
    except Exception:
        return None


def _text_of(content) -> str | None:
    """从 content 块列表提取纯文本"""
    if isinstance(content, str):
        return content.strip() or None
    if isinstance(content, list):
        parts = [b.get("text") for b in content if isinstance(b, dict) and b.get("type") == "text" and b.get("text")]
        return " ".join(parts).strip() if parts else None
    return None


def parse_date(s: str) -> datetime.date | None:
    """解析日期参数：YYYY-MM-DD / today / yesterday / N 天前（如 -1）"""
    s = (s or "").strip().lower()
    if not s:
        return None
    today = datetime.date.today()
    if s == "today":
        return today
    if s == "yesterday":
        return today - datetime.timedelta(days=1)
    if s in ("前天", "the day before yesterday"):
        return today - datetime.timedelta(days=2)
    try:
        if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", s):
            return datetime.date.fromisoformat(s)
    except ValueError:
        return None
    m = re.fullmatch(r"(-?\d+)", s)
    if m:
        return today + datetime.timedelta(days=int(m.group(1)))
    return None


def lane5(date: datetime.date | None, limit: int = 40) -> list:
    """L5 时间窗召回：扫描 DSH 会话 JSONL 原文，按消息 time 字段过滤指定日期。

    一手材料溯源：消息级时间戳（epoch ms），只取 user/assistant 文本消息；
    自动化/子代理会话（dsh-automation-session-*）不再跳过（2026-08-21 修正，
    借鉴 OpenClaw memory-architecture：任务产物可被检索；自动提升排除在提案侧）。
    """
    if date is None or not SESSIONS_ROOT.exists():
        return []
    out = []
    target_start = datetime.datetime.combine(date, datetime.time.min)
    target_end = datetime.datetime.combine(date, datetime.time.max)
    try:
        ws_dirs = sorted(SESSIONS_ROOT.iterdir())
    except Exception:
        return []
    for ws in ws_dirs:
        if not ws.is_dir():
            continue
        try:
            sess_dirs = sorted(ws.iterdir())
        except Exception:
            continue
        for sess in sess_dirs:
            if not sess.is_dir():
                continue
            log = _session_log(sess)
            if not log:
                continue
            raw = _unzstd(log)
            if not raw:
                continue
            for line in raw.splitlines():
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                t = ev.get("type")
                if t not in ("user/message", "assistant/message"):
                    continue
                ts = ev.get("time")
                if not ts:
                    continue
                try:
                    dt = datetime.datetime.fromtimestamp(ts / 1000)
                except Exception:
                    continue
                if not (target_start <= dt <= target_end):
                    continue
                data = ev.get("data") or {}
                if t == "user/message":
                    src = data.get("source") or {}
                    if src.get("kind") != "user":
                        continue  # 只取真实用户消息，跳过插件/系统注入（召回回路防护）
                text = _text_of(data.get("content") if t == "user/message" else (data.get("message") or {}).get("content"))
                if not text or text.startswith("Current runtime context"):
                    continue
                stamp = dt.strftime("%H:%M")
                out.append((1.0, f"[{t[:4]} {stamp}] {text[:500]}"))
            if len(out) >= limit:
                return out[:limit]
    return out[:limit]


WIKI_DB = HOME / ".dsh" / "memories" / "wiki_index.sqlite"


def _split_query_terms(query: str) -> list:
    """拆查询词为可 MATCH 的短语：拉丁/数字词原样（≥3）；中文连续串切 3 字符滑动窗口。
    返回 (match_terms, like_terms)：match_terms 用于 FTS5，like_terms（<3 字符）用于 LIKE 兜底。"""
    match_terms, like_terms = [], []
    for tok in re.split(r"[\s,，。;；:：()（）!?！？/\\\-_=+]+", query):
        tok = tok.strip()
        if not tok:
            continue
        if re.search(r"[\u4e00-\u9fff]", tok):
            n = len(tok)
            if n < 3:
                like_terms.append(tok)
            else:
                for i in range(n - 2):
                    match_terms.append(tok[i:i + 3])
        elif len(tok) >= 3:
            match_terms.append(tok)
        else:
            like_terms.append(tok)
    return match_terms, like_terms


def _wiki_index_hits(query: str, limit: int = 4) -> list:
    """查 wiki 全库索引（wiki_indexer.py 构建，trigram）：查询词分词+滑动窗口 OR 匹配，
    多词命中优先排序；<3 字符词 LIKE 兜底。返回 [(rel_path, title), ...]。"""
    if not query or not query.strip() or not WIKI_DB.exists():
        return []
    try:
        import sqlite3
        con = sqlite3.connect(WIKI_DB)
        match_terms, like_terms = _split_query_terms(query)
        scores = {}
        for t in match_terms:
            try:
                rows = con.execute(
                    "SELECT rel_path FROM wiki_fts WHERE wiki_fts MATCH ?",
                    (f'"{t}"',)).fetchall()
            except Exception:
                rows = []
            for (rel,) in rows:
                scores[rel] = scores.get(rel, 0) + 1
        if like_terms:
            like_rows = con.execute(
                "SELECT rel_path FROM wiki_docs WHERE title LIKE ? OR frontmatter LIKE ? OR first_para LIKE ?",
                (f"%{like_terms[0]}%", f"%{like_terms[0]}%", f"%{like_terms[0]}%")).fetchall()
            for (rel,) in like_rows:
                scores[rel] = scores.get(rel, 0) + 1
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])[:limit]
        out = []
        for rel, _sc in ranked:
            t = con.execute("SELECT title FROM wiki_docs WHERE rel_path=?", (rel,)).fetchone()
            out.append((rel, t[0] if t else rel))
        return out
    except Exception:
        return []


# ── BFS 沿 wikilink 扩展（2026-08-23，借鉴 TencentDB-Agent-Memory graph-search） ──
# 原则：seed 命中 → 沿 wiki_links 边（[[wikilink]]）向外扩展，hop 每跳衰减；只返回
# 标题（详情留待调用方决定是否展开正文），压缩注入体积。
def _wiki_bfs_titles(seeds: list, hop: int = 1, decay: float = 0.5, max_nodes: int = 30) -> list:
    """从 seed 标题出发 BFS 沿 wikilink 扩展，返回 [(title, hop, score), ...]（按分数降序）。

    - seeds: 起始 title 列表（可含 rel_path 混用，函数内解析）
    - hop: 最大扩展跳数（1 = 只取 seed 的直接邻居；0 = 只返回 seed 本身）
    - decay: 每跳分数衰减系数
    - max_nodes: 访问节点硬上限（防稠密图爆炸）
    """
    if not WIKI_DB.exists():
        return []
    import sqlite3
    try:
        con = sqlite3.connect(WIKI_DB)
    except Exception:
        return []
    # 解析 seed → title（rel_path 或 title 均可）
    seed_titles = []
    for s in seeds:
        if not s:
            continue
        row = con.execute("SELECT title FROM wiki_docs WHERE rel_path=?", (s,)).fetchone()
        if row:
            seed_titles.append(row[0])
        elif con.execute("SELECT 1 FROM wiki_docs WHERE title=?", (s,)).fetchone():
            seed_titles.append(s)
    if not seed_titles:
        return []
    # BFS
    best: dict = {}          # title -> score（取最高分路径）
    min_hop: dict = {}       # title -> 最小 hop
    frontier = []
    for t in seed_titles:
        best[t] = max(best.get(t, 0.0), 1.0)
        min_hop[t] = 0
        frontier.append((t, 0))
    visited = set(seed_titles)
    while frontier and len(visited) < max_nodes:
        nxt = []
        for title, h in frontier:
            if h >= hop:
                continue
            rows = con.execute(
                "SELECT DISTINCT dst_title FROM wiki_links WHERE src_id IN "
                "(SELECT id FROM wiki_docs WHERE title=?)", (title,)).fetchall()
            for (dst,) in rows:
                if dst in visited:
                    continue
                visited.add(dst)
                nh = h + 1
                score = 1.0 * (decay ** nh)
                if score > best.get(dst, 0.0):
                    best[dst] = score
                    min_hop[dst] = nh
                nxt.append((dst, nh))
        frontier = nxt
    # 反向边也走一跳（邻居指向当前 seed 的也值得带出）——仅 hop=1 时做一次
    if hop >= 1:
        for t in seed_titles:
            rows = con.execute(
                "SELECT DISTINCT src.title FROM wiki_links l JOIN wiki_docs src ON l.src_id=src.id "
                "JOIN wiki_docs dst ON dst.title=l.dst_title WHERE dst.title=?", (t,)).fetchall()
            for (src_title,) in rows:
                if src_title in visited:
                    continue
                visited.add(src_title)
                score = 1.0 * decay
                if score > best.get(src_title, 0.0):
                    best[src_title] = score
                    min_hop[src_title] = 1
    out = [(t, min_hop.get(t, 0), s) for t, s in best.items()]
    out.sort(key=lambda x: (-x[2], x[1]))
    return out[:max_nodes]


def lane4(q_entities: set, budget: int, query: str = "") -> list:
    """L4 wiki 通道：① wiki 全库索引检索（主入口，trigram，不依赖 L1 指针——L1 预算放不下全库指针）；
    ② BFS 沿 wikilink 扩展 seed 关联页（2026-08-23，借鉴 TencentDB-Agent-Memory graph-search）；
    ③ L1 条目 `→ wiki/路径` 活指针跟随（兼容保留，模板/目录指针支持）。"""
    out = []
    seen = set()
    # ① 全库索引检索（seed）
    seeds = []
    for rel, title in _wiki_index_hits(query, limit=4):
        if rel in seen:
            continue
        seen.add(rel)
        seeds.append(title)
        for target in _resolve_wiki(rel):
            try:
                text = target.read_text(errors="ignore")
            except Exception:
                continue
            rel_show = str(target.relative_to(WIKI_ROOT.parent))
            out.append((round(1.0, 2), f"[L4 wiki:{rel_show}] {title} | {text[:budget]}"))
            break
    # ② BFS 沿 wikilink 扩展（只列标题，不展开正文；hop=1 取直接邻居，省 token）
    if seeds:
        for title, hop, score in _wiki_bfs_titles(seeds, hop=1, decay=0.5, max_nodes=12):
            if title in seeds:
                continue  # seed 本身已返回
            row = _title_to_rel(title)
            if not row:
                continue
            rel = row
            if rel in seen:
                continue
            seen.add(rel)
            out.append((round(score, 2), f"[L4 wiki邻接 hop{hop}] {title}（{rel}）"))
    # ③ L1 活指针跟随（原有逻辑）
    for ent in load_l1():
        if not (q_entities & set(ent["entities"])):
            continue
        for m in re.finditer(r"→\s*wiki/([^\s`]+)", ent["text"]):
            rel = m.group(1).rstrip('。，；;')
            for target in _resolve_wiki(rel):
                rel_show = str(target.relative_to(WIKI_ROOT.parent))
                if rel_show in seen:
                    continue
                seen.add(rel_show)
                try:
                    text = target.read_text(errors="ignore")
                except Exception:
                    continue
                out.append((round(1.0, 2), f"[L4 wiki:{rel_show}] {text[:budget]}"))
    return out


CURATED_REL = WIKI_ROOT.parent / "_meta" / "relations.md"  # wiki/_meta/relations.md


def _curated_edges():
    """解析 wiki/_meta/relations.md 关系边三元组（markdown 表格行）。
    单元格可含注解/多实体（如 '句号/刘兰/藏妤/何启敏'、'老郑（主攻线）'）。"""
    if not CURATED_REL.exists():
        return []
    try:
        text = CURATED_REL.read_text(errors="ignore")
    except Exception:
        return []
    out = []
    in_edges = False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("## 关系边"):
            in_edges = True
            continue
        if not in_edges or not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 4:
            continue
        a, rel, b, ev = cells[0], cells[1], cells[2], cells[3]
        if a.startswith("实体A") or a.startswith("---"):
            continue
        names_a = [e for e, _ in _ALL_ENTITIES if e in a]
        names_b = [e for e, _ in _ALL_ENTITIES if e in b]
        out.append({"a": a, "rel": rel, "b": b, "evidence": ev,
                    "names_a": names_a, "names_b": names_b})
    return out


def lane6(query: str, q_entities: set, budget: int) -> list:
    """L6 关系图谱：curated 类型边（wiki/_meta/relations.md）+ 自动共现边（relations.json）。
    关系型查询（≥2 实体命中 或 含关系标记词）时，返回命中实体的邻接边 + 证据。
    双源缺失/无命中 → 空路，零影响。"""
    rel_markers = re.search(r"关系|联系|关联|哪些|谁|参与|连着|相关|什么|哪个|工作|单位|在哪|公司|任职|认识|介绍|一起|做过", query)
    if len(q_entities) < 2 and not rel_markers:
        return []
    if not q_entities:
        return []
    out = []
    # curated 类型边（优先展示）
    for e in _curated_edges():
        if (set(e["names_a"]) & q_entities) or (set(e["names_b"]) & q_entities):
            out.append((1.0, f"[L6 关系图谱·curated] {e['a']} ─({e['rel']})→ {e['b']} | 证据: {e['evidence']}"))
            if len(out) >= 10:
                return out
    # 自动共现边（兜底/全量邻接）
    gf = MEMORIES.parent / "data" / "relations.json"
    if gf.exists():
        try:
            g = json.loads(gf.read_text())
        except Exception:
            g = {}
        nodes = {n["name"] for n in g.get("nodes", [])}
        hits = q_entities & nodes
        for e in g.get("edges", []):
            if e["source"] in hits or e["target"] in hits:
                srcs = " | ".join(f"{ev['src']}@{ev['date']}" for ev in e["evidence"][:2])
                out.append((1.0, f"[L6 关系图谱] {e['source']} ── {e['target']} (权重{e['weight']}) | 证据: {srcs}"))
                if len(out) >= 12:
                    break
    return out


def rrf_fuse(lists: list, weights: list | None = None) -> list:
    """lists: 每路 [(score, text) ...]；以文本内容为键做 RRF 融合（同内容多路命中叠加）。
    weights: 每路权重（默认 1.0），用于时间性查询抬高 L5 一手材料、压制 L3 旧摘要。"""
    scores = defaultdict(float)
    meta = {}
    for lane_idx, lane in enumerate(lists):
        w = weights[lane_idx] if lane_idx < len(weights) else 1.0
        for rank, (score, text) in enumerate(lane, 1):
            key = text  # 内容即身份：同文本多路命中叠加分数，不同文本不冲突
            scores[key] += w * (1.0 / (K + rank))
            meta[key] = {"lane": lane_idx, "text": text}
    ranked = sorted(scores.items(), key=lambda x: -x[1])
    return [(key, round(score, 4), meta[key]) for key, score in ranked]


# ---------- 记忆命中统计（借鉴 hermes memory_hits：存了不用要有度量） ----------
def record_hits(results: list, enabled: bool = True, inject_chars: int = 0) -> None:
    """每次真实召回把命中条目计数落库（hits.sqlite）。

    key = 文本 sha1 前 16 位（内容即身份）；同文本跨会话累计 count。
    L1 命中以条目前缀为身份——memory_hit_audit.py 用前缀匹配反查「从未命中条目」。
    primer 高频注入调用传 enabled=False（--no-hits），避免刷屏污染统计。
    inject_chars（2026-08-22）：本次实际注入字符数，供预算档位调优统计。
    """
    if not enabled or not results:
        return
    try:
        con = sqlite3.connect(HITS_DB)
        con.execute("""CREATE TABLE IF NOT EXISTS hits(
            lane TEXT, key TEXT, text TEXT, count INTEGER DEFAULT 1,
            last_ts TEXT, inject_chars INTEGER DEFAULT 0,
            PRIMARY KEY(lane, key))""")
        # 旧表迁移（2026-08-22）：已有 hits 表无 inject_chars 列 → ALTER 补列
        cols = [d[1] for d in con.execute("PRAGMA table_info(hits)").fetchall()]
        if "inject_chars" not in cols:
            con.execute("ALTER TABLE hits ADD COLUMN inject_chars INTEGER DEFAULT 0")
        now = datetime.datetime.now().isoformat(timespec="seconds")
        for r in results:
            key = hashlib.sha1(r["text"].encode("utf-8")).hexdigest()[:16]
            con.execute(
                "INSERT INTO hits(lane, key, text, count, last_ts, inject_chars) VALUES(?,?,?,1,?,?) "
                "ON CONFLICT(lane, key) DO UPDATE SET count = count + 1, last_ts = excluded.last_ts, inject_chars = excluded.inject_chars",
                (r["source"], key, r["text"][:200], now, inject_chars),
            )
        con.commit()
        con.close()
    except Exception:
        pass  # 命中统计失败不影响召回主链路


# ---------- 事实速查卡注入（借鉴 hermes fidelity-primer：定型卡片优先于模糊召回） ----------
FIDELITY_CARD_FILE = WIKI_ROOT.parent / "raw" / "技术" / "系统运维" / "错误事项清单-dsh.md"


def _strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        m = re.match(r"^---\n.*?\n---\n", text, re.S)
        if m:
            return text[m.end():]
    return text


def _extract_fidelity_card(path) -> str | None:
    """取错误事项清单顶部 <!-- fidelity-card --> 区块"""
    try:
        text = path.read_text(errors="ignore")
    except Exception:
        return None
    m = re.search(r"<!--\s*fidelity-card\s*-->(.*?)<!--\s*/fidelity-card\s*-->", text, re.S)
    if m:
        return m.group(1).strip()
    return None


def lane_primers(q_entities: set, query: str, max_cards: int = 2) -> list:
    """事实速查卡（fidelity primer）：命中实体时优先注入定型卡片而非模糊 RRF 结果。

    卡片源：
      1. L1 条目中的 `→ wiki/...` 活指针（人物分析档案头/系统状态卡——硬事实真值源）
      2. 错误/故障/异常类查询 → 错误事项清单顶部 fidelity-card 事件速查卡
    去重：按内容首 40 字符；预算：≤2 卡，每卡 ≤600 字符。
    """
    cards = []
    seen = set()
    # 1. wiki 指针卡
    for ent in load_l1():
        if not (q_entities & set(ent["entities"])):
            continue
        for m in re.finditer(r"→\s*wiki/([^\s`]+)", ent["text"]):
            rel = m.group(1).rstrip('。，；;')
            for target in _resolve_wiki(rel):
                try:
                    text = _strip_frontmatter(target.read_text(errors="ignore"))
                except Exception:
                    continue
                head = text.strip()[:600]
                if not head or head[:40] in seen:
                    continue
                seen.add(head[:40])
                cards.append((f"速查卡 {target.name}", head))
                if len(cards) >= max_cards:
                    return cards
    # 2. 错误事项 fidelity-card（错误/故障类查询触发）
    if len(cards) < max_cards and re.search(r"错误|故障|报错|异常|失败|踩坑|崩|挂", query):
        card = _extract_fidelity_card(FIDELITY_CARD_FILE)
        if card and card[:40] not in seen:
            cards.append(("事件速查卡 错误事项清单", card[:600]))
    return cards[:max_cards]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("--budget", type=int, default=800)
    ap.add_argument("--topk", type=int, default=5)
    ap.add_argument("--l3", default=None)
    ap.add_argument("--date", default=None, help="时间窗召回（L5）：YYYY-MM-DD / today / yesterday / -N 天前")
    ap.add_argument("--deep", action="store_true", help="深度召回（通道2 升级）：topk=8、预算=2000、均分截断")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--primers", action="store_true", help="事实速查卡注入模式（fidelity-primer）：卡片优先于 RRF 结果")
    ap.add_argument("--no-hits", action="store_true", help="不记录命中统计（primer 高频注入调用使用）")
    args = ap.parse_args()

    if args.deep:
        args.topk = max(args.topk, 8)
        args.budget = max(args.budget, 2000)

    q_entities = {e for e, _ in extract_entities(args.query)}
    q_entities |= {t for t in re.split(r"[\s,，。]+", args.query) if t in _KNOWN_PERSONS or t in _KNOWN_TOPICS or t in _KNOWN_PROJECTS}

    # fidelity-primer：命中实体且有速查卡 → 直接输出定型卡片（不跑 RRF）
    if args.primers:
        cards = lane_primers(q_entities, args.query)
        if cards:
            print(f"# 速查卡: {args.query}  实体: {sorted(q_entities)}")
            for title, card in cards:
                print(f"[事实速查卡 · {title}]")
                print(card)
                print()
            return
        # 无卡降级为常规召回（维持既有注入能力）

    l1 = lane1(args.query, q_entities)
    l2_raw = lane2(args.query)
    corrections = load_corrected()
    l2 = [(1.0, (f"⚠曾被纠正 {t}" if is_corrected(t, corrections) else t)) for t in l2_raw]
    l3 = lane3_mcp(args.query) if not args.l3 else lane3(args.l3)
    l4 = lane4(q_entities, max(200, args.budget // 4), args.query)

    # L5 时间窗召回（--date 显式指定；或 query 含时间表述自动触发）
    date = parse_date(args.date)
    if date is None and re.search(r"昨天|前天|今天|最近|上周|前天|几号|哪天|日期", args.query):
        date = parse_date("yesterday") if "昨天" in args.query else parse_date("today")
    l5 = lane5(date)
    # 时间性查询：L5 一手材料优先 ×5，L3 语义旧摘要 ×0.3 压制
    weights = [1.0, 1.0, 1.0, 1.0, 1.0, 0.7]
    if date is not None and l5:
        weights[2] = 0.3
        weights[4] = 5.0

    l6 = lane6(args.query, q_entities, args.budget)

    lanes = [l1, l2, l3, l4, l5, l6]
    fused = rrf_fuse(lanes, weights)

    results = []
    used = 0
    for key, score, meta in fused[: args.topk]:
        text = meta["text"]
        lane_name = ["L1", "L2", "L3", "L4", "L5", "L6"][meta["lane"]]
        trust = {"L1": "高", "L2": "中", "L3": "低", "L4": "中", "L5": "高", "L6": "中"}[lane_name]
        snippet = text[: max(1, args.budget - used)]
        used += len(snippet)
        results.append({"source": lane_name, "score": score, "trust": trust, "text": snippet})

    # 记忆命中统计（② memory_hits）：真实召回落库，primer 注入不记录
    record_hits(results, enabled=not args.no_hits, inject_chars=used)

    if args.json:
        print(json.dumps({"query": args.query, "entities": sorted(q_entities),
                          "date": date.isoformat() if date else None, "results": results}, ensure_ascii=False))
        return

    print(f"# 召回: {args.query}  实体: {sorted(q_entities)}  时间窗: {date.isoformat() if date else '—'}")
    if not results:
        print("（无命中）")
        return
    for i, r in enumerate(results, 1):
        print(f"[RRF #{i} · {r['source']} · 可信度: {r['trust']}]")
        print(r["text"])
        print()


if __name__ == "__main__":
    main()
