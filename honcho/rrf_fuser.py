"""
RRF Fusion Layer for Honcho Memory Provider
============================================
Implements four-way RRF multi-recall fusion + entity graph + evidence ledger
as described in the HMS architecture.

Sources:
  L1: MEMORY.md / USER.md keyword matching (curated facts) + reference following
  L2: session_search FTS5 (historical conversations, real message content)
      + ReFind (arXiv:2608.12888) ablation mechanisms:
      session aggregation two-level ranking, ±2 context expansion,
      optional date_from/date_to time filtering
  L3: Honcho semantic recall (via prefetch result)
  L4: Obsidian wiki full-text search (knowledge base, line-level location)

Usage:
  from plugins.memory.honcho.rrf_fuser import rrf_fuse_prefetch
  fused = rrf_fuse_prefetch(query, honcho_context)
  fused = rrf_fuse_prefetch(query, honcho_context, date_from='2026-10-01')
"""

import re
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Tuple

# ---------------------------------------------------------------------------
# Paths (dsh 版，2026-08-19 解耦：由 hermes 迁移，指向 dsh 记忆库)
# ---------------------------------------------------------------------------
def _hermes_home() -> Path:
    return Path.home() / '.dsh'

def _wiki_root() -> Path:
    return Path.home() / 'Nutstore Files' / '我的坚果云' / 'wiki'

def _memory_dir() -> Path:
    return Path.home() / '.dsh' / 'memories'

# ---------------------------------------------------------------------------
# Hard-coded entity lists (extend as needed)
# ---------------------------------------------------------------------------
_KNOWN_PERSONS = frozenset({'老郑', '陈丹', '句号', '藏妤', '刘兰', '何启敏', 'sophia'})
_KNOWN_PROJECTS = frozenset({'HMS', 'Shadow-Weave', 'RRF', 'Hermes', 'Mem0', 'Letta', 'Memory-OS'})
_KNOWN_TOPICS = frozenset({'A股', '情感', '控制论', '会议纪要', 'Vaultwarden', 'Honcho', '零信任', 'RDP', 'ZeroTier'})

_ALL_ENTITIES = {
    *(p.lower() for p in _KNOWN_PERSONS),
    *(p.lower() for p in _KNOWN_PROJECTS),
    *(p.lower() for p in _KNOWN_TOPICS),
}

def extract_entities(text: str) -> List[Tuple[str, str]]:
    """Extract known entities from text. Uses 'in' operator (not \\b) for CJK."""
    text_lower = text.lower()
    entities = []
    for name in sorted(_ALL_ENTITIES, key=len, reverse=True):
        if name in text_lower:
            for case in (_KNOWN_PERSONS | _KNOWN_PROJECTS | _KNOWN_TOPICS):
                if case.lower() == name:
                    label = case
                    break
            else:
                label = name
            if label in _KNOWN_PERSONS:
                entities.append((label, 'person'))
            elif label in _KNOWN_PROJECTS:
                entities.append((label, 'project'))
            else:
                entities.append((label, 'topic'))
    return entities


# ---------------------------------------------------------------------------
# Tokenizer: returns (primaries, grams)
#   primaries: whole chunks (≥2 chars) split by punctuation/space
#   grams: 2-char CJK sliding windows from CJK chunks (for substring recall)
# ---------------------------------------------------------------------------
# Generic 2-char CJK stopwords — never useful for recall, always noise
_STOP_GRAMS = frozenset({
    '今天', '天大', '盘怎', '怎么', '么样', '最近', '近聊', '聊得', '得怎',
    '现在', '可以', '这个', '那个', '还是', '就是', '觉得', '知道', '应该', '需要',
    '什么', '我们', '你们', '他们', '自己', '没有', '不是', '一下', '一个', '一次',
    '一点', '这样', '那样', '之后', '之前', '上面', '下面', '里面', '外面', '然后',
    '而且', '但是', '因为', '所以', '如果', '虽然', '由于', '关于', '对于', '通过',
    '根据', '进行', '已经', '正在', '将要', '开始', '结束', '完成', '继续', '时候',
    '情况', '问题', '事情', '东西', '有人', '大家', '别的', '其它', '这些', '那些',
})

# Longer primaries that are pure filler
_STOP_PRIMARY = frozenset({
    '怎么样', '什么样', '为什么', '怎么办', '怎么样啦', '怎么样啊', '怎么样呢',
    '今天的', '现在的', '最近的', '有没有', '是不是', '行不行', '好不好', '可不可以',
})

def _tokenize(query: str) -> Tuple[List[str], List[str]]:
    query_lower = query.lower()
    raw_terms = [t for t in re.split(r'[\s,，。、；;：:！!？?（）()【】\[\]\"\'\-_]', query_lower) if len(t) >= 2]
    primaries: List[str] = []
    grams: List[str] = []
    _stop_pattern = '|'.join(sorted(_STOP_GRAMS, key=len, reverse=True))
    for t in raw_terms:
        if t not in _STOP_PRIMARY:
            primaries.append(t)
        # Extract pure-CJK runs from the chunk for 2-gram windows
        for cjk_run in re.findall(r'[\u4e00-\u9fff]{4,}', t):
            grams.extend(g for g in (cjk_run[i:i+2] for i in range(len(cjk_run) - 1))
                         if g not in _STOP_GRAMS)
            # Split CJK run by stopwords to recover meaningful chunks
            # e.g. '股今天大盘怎么样' → ['股', '大盘']  (chunks ≥2 chars kept)
            for chunk in re.split(_stop_pattern, cjk_run):
                if len(chunk) >= 2 and chunk not in _STOP_PRIMARY:
                    primaries.append(chunk)
    # Dedup, keep order
    seen = set()
    primaries_out = []
    for t in primaries:
        if t not in seen:
            seen.add(t)
            primaries_out.append(t)
    seen = set()
    grams_out = []
    for g in grams:
        if g not in seen:
            seen.add(g)
            grams_out.append(g)
    return primaries_out[:12], grams_out[:16]


# ---------------------------------------------------------------------------
# RRF fusion
# ---------------------------------------------------------------------------
def rrf_fuse(ranks_dict: Dict[str, List[str]], k: int = 60) -> List[Tuple[str, float]]:
    """Reciprocal Rank Fusion."""
    scores = defaultdict(float)
    for source, items in ranks_dict.items():
        for rank, item_id in enumerate(items, 1):
            scores[item_id] += 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


# ---------------------------------------------------------------------------
# L1 keyword matching against built-in memory files
# ---------------------------------------------------------------------------
def _l1_keyword_match(query: str, top_k: int = 10) -> List[str]:
    """Keyword match against MEMORY.md / USER.md entries.

    Scores by occurrence count: primary terms weigh 3x, 2-grams weigh 1x.
    This keeps entity-heavy entries (e.g. 陈丹) above generic noise (最近).
    """
    primaries, grams = _tokenize(query)
    # Inject known entity names (e.g. 'A股' survives mixed-token queries)
    for ent_name, _ent_type in extract_entities(query):
        ent_lower = ent_name.lower()
        if ent_lower not in primaries:
            primaries.append(ent_lower)
    if not primaries and not grams:
        return []

    memory_dir = _memory_dir()
    hits = []
    for fname in ['MEMORY.md', 'USER.md']:
        fpath = memory_dir / fname
        if not fpath.exists():
            continue
        content = fpath.read_text(encoding='utf-8')
        entries = content.split('\n§\n')
        for i, entry in enumerate(entries):
            entry_lower = entry.lower()
            score = 0
            for t in primaries:
                score += entry_lower.count(t) * 3
            for g in grams:
                score += entry_lower.count(g)
            if score > 0:
                hits.append((score, f"{fname}:{i}"))
    hits.sort(key=lambda x: x[0], reverse=True)
    return [h[1] for h in hits[:top_k]]


# ---------------------------------------------------------------------------
# L2 session_search via SQLite FTS5 (with real content resolution)
# ReFind (arXiv:2608.12888) ablation-backed mechanisms, all inside this source:
#   1. session aggregation two-level ranking (message rank r1 + session rank r2,
#      RRF-fused with the same k=60 as the main fusion)
#   2. ±2 context expansion around each direct hit (marked via `sessionctx:` ids,
#      rendered as [上下文扩展] in the evidence ledger)
#   3. optional date_from/date_to ISO time filter on chunk_meta.observed_at
# ---------------------------------------------------------------------------
def _normalize_iso_bound(value, is_end: bool):
    """Normalize an ISO date/datetime bound to ISO text for lexicographic
    comparison against chunk_meta.observed_at.

    Accepts '2026-10-01' or '2026-10-01T00:00:00'. A date-only date_to is
    inclusive of the whole day (23:59:59.999999). None/empty/unparseable → None.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if is_end and 'T' not in s and ' ' not in s:
        dt = dt.replace(hour=23, minute=59, second=59, microsecond=999999)
    return dt.isoformat()


def _l2_two_level_rank(rows: List, top_k: int, k: int = 60) -> List:
    """Session aggregation two-level ranking (ReFind).

    rows are FTS5 hits in bm25 order. Message-level rank r1 comes from that
    order; each session's score is the sum of its member messages' RRF scores,
    giving session-level rank r2. A message's final score is
    1/(k+r1) + 1/(k+r2_of_its_session) — messages inherit their session's rank.
    """
    msg_score: Dict[object, float] = {}
    sessions: Dict[object, List] = defaultdict(list)
    row_session: Dict[object, object] = {}
    for r1, row in enumerate(rows, 1):
        rid = row['rowid']
        msg_score[rid] = 1.0 / (k + r1)
        skey = row['session'] if row['session'] is not None else ('__none__', rid)
        row_session[rid] = skey
        sessions[skey].append(rid)
    sess_ranked = sorted(
        sessions.items(),
        key=lambda kv: sum(msg_score[rid] for rid in kv[1]),
        reverse=True,
    )
    sess_rank = {skey: r2 for r2, (skey, _members) in enumerate(sess_ranked, 1)}

    def final_score(row) -> float:
        rid = row['rowid']
        return msg_score[rid] + 1.0 / (k + sess_rank[row_session[rid]])

    return sorted(rows, key=final_score, reverse=True)[:top_k]


def _l2_session_search(query: str, top_k: int = 10,
                       date_from=None, date_to=None) -> List[str]:
    """Search session history via FTS5. Returns session message row IDs.

    Direct hits are `session:<rowid>` (two-level session-aggregated ranking);
    ±2 same-session neighbours are appended as `sessionctx:<rowid>`.
    date_from/date_to (ISO date or datetime strings) restrict hits to
    chunk_meta.observed_at within [date_from, date_to]; omitted → no filtering.
    """
    db_path = _memory_dir() / 'index.sqlite'
    if not db_path.exists():
        return []

    primaries, grams = _tokenize(query)
    fts_terms = [t for t in primaries + grams if len(t) >= 2]
    if not fts_terms:
        return []

    from_iso = _normalize_iso_bound(date_from, is_end=False)
    to_iso = _normalize_iso_bound(date_to, is_end=True)

    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r['name'] for r in cursor.fetchall()]
        if 'session_chunks' not in tables:
            conn.close()
            return []

        # dsh 索引：session_chunks FTS5(text, session, seq)
        fts_query = ' OR '.join(f'"{t}"' for t in fts_terms)
        try:
            if from_iso or to_iso:
                # Time filter joins the observed_at side table. FTS5 MATCH and
                # bm25() require the table name, not the alias.
                sql = ('SELECT sc.rowid, sc.session, sc.seq FROM session_chunks sc '
                       'JOIN chunk_meta cm ON cm.rowid = sc.rowid '
                       'WHERE session_chunks MATCH ?')
                params: list = [fts_query]
                if from_iso:
                    sql += ' AND cm.observed_at >= ?'
                    params.append(from_iso)
                if to_iso:
                    sql += ' AND cm.observed_at <= ?'
                    params.append(to_iso)
                sql += ' ORDER BY bm25(session_chunks) LIMIT ?'
            else:
                sql = ('SELECT rowid, session, seq FROM session_chunks '
                       'WHERE session_chunks MATCH ? ORDER BY bm25(session_chunks) LIMIT ?')
                params = [fts_query]
            # Wider candidate pool so session aggregation has signal to work with.
            cursor.execute(sql, (*params, top_k * 4))
            rows = cursor.fetchall()
        except sqlite3.OperationalError:
            rows = []

        if not rows:
            conn.close()
            return []

        ranked = _l2_two_level_rank(rows, top_k)
        direct_ids = [f"session:{r['rowid']}" for r in ranked]
        seen = {r['rowid'] for r in ranked}

        # Context expansion: ±2 same-session neighbours of each direct hit
        # (truncated at session boundaries by the session=BETWEEN predicate).
        expanded_ids: List[str] = []
        for r in ranked:
            session = r['session']
            try:
                seq = int(r['seq'])
            except (TypeError, ValueError):
                continue
            if session is None:
                continue
            try:
                cursor.execute(
                    'SELECT rowid FROM session_chunks WHERE session = ? '
                    'AND CAST(seq AS INTEGER) BETWEEN ? AND ? '
                    'ORDER BY CAST(seq AS INTEGER)',
                    (session, seq - 2, seq + 2),
                )
                for nb in cursor.fetchall():
                    nrid = nb['rowid']
                    if nrid in seen:
                        continue
                    seen.add(nrid)
                    expanded_ids.append(f"sessionctx:{nrid}")
            except sqlite3.OperationalError:
                pass
        conn.close()
        return direct_ids + expanded_ids

    except Exception:
        return []


# ---------------------------------------------------------------------------
# L3: parse Honcho's existing prefetch context
# ---------------------------------------------------------------------------
_L3_PARAS: List[str] = []

def _l3_parse_honcho(honcho_context: str, top_k: int = 10) -> List[str]:
    """Extract semantic recall items from Honcho's prefetch output."""
    global _L3_PARAS
    if not honcho_context or not honcho_context.strip():
        return []
    paragraphs = [p.strip() for p in honcho_context.split('\n\n') if p.strip()]
    _L3_PARAS = paragraphs
    return [f"honcho:{i}" for i in range(min(len(paragraphs), top_k))]


# ---------------------------------------------------------------------------
# L4: Obsidian wiki full-text search (line-level location, D3)
# ---------------------------------------------------------------------------
_WIKI_CACHE: Dict[str, Tuple[float, str]] = {}


def _wiki_hit_line_span(head: str, primaries: List[str], grams: List[str]):
    """1-based (first, last) lines of `head` containing any term, else None."""
    first = last = None
    for i, line in enumerate(head.split('\n'), 1):
        ll = line.lower()
        if any(t in ll for t in primaries) or any(g in ll for g in grams):
            if first is None:
                first = i
            last = i
    return (first, last) if first is not None else None


def _l4_wiki_search(query: str, top_k: int = 6) -> List[str]:
    """Search Obsidian wiki (raw/ + concepts/) for relevant notes.

    Scores by occurrence count (same weighting as L1) so entity-dense notes
    rank above generic matches. Hit items carry the matched line range:
    `wiki:<rel>:<start_line>-<end_line>` (plain `wiki:<rel>` when only the
    filename matched).
    """
    wiki_root = _wiki_root()
    if not wiki_root.exists():
        return []

    primaries, grams = _tokenize(query)
    # Inject known entity names (same as L1)
    for ent_name, _ent_type in extract_entities(query):
        ent_lower = ent_name.lower()
        if ent_lower not in primaries:
            primaries.append(ent_lower)
    if not primaries and not grams:
        return []

    hits = []
    for area in ['raw', 'concepts']:
        base = wiki_root / area
        if not base.exists():
            continue
        for p in base.rglob('*.md'):
            try:
                mtime = p.stat().st_mtime
                cache = _WIKI_CACHE.get(str(p))
                if cache and cache[0] >= mtime:
                    head = cache[1]
                else:
                    head = p.read_text(encoding='utf-8', errors='ignore')[:3000]
                    _WIKI_CACHE[str(p)] = (mtime, head)
            except OSError:
                continue
            head_lower = head.lower()
            name_lower = p.name.lower()
            score = 0
            for t in primaries:
                score += (head_lower.count(t) + name_lower.count(t)) * 3
            for g in grams:
                score += head_lower.count(g)
            if score >= 2:  # threshold: filter single generic-gram hits
                rel = p.relative_to(wiki_root)
                span = _wiki_hit_line_span(head, primaries, grams)
                if span:
                    hits.append((score, f"wiki:{rel}:{span[0]}-{span[1]}"))
                else:
                    hits.append((score, f"wiki:{rel}"))
    hits.sort(key=lambda x: x[0], reverse=True)
    return [h[1] for h in hits[:top_k]]


# ---------------------------------------------------------------------------
# Evidence ledger assembly (with reference following)
# ---------------------------------------------------------------------------
_WIKI_RANGE_RE = re.compile(r'^(?P<path>.+):(?P<start>\d+)-(?P<end>\d+)$')


def _read_wiki_summary(rel_str: str, max_chars: int = 400) -> str:
    """Read a wiki file by relative path, return first lines summary.

    Accepts both 'raw/...' and 'wiki/raw/...' (strips redundant prefix),
    and an optional line-range suffix 'raw/x.md:3-9' (D3 line-level location):
    the summary is then sliced to those lines and annotated with the range.
    """
    if rel_str.startswith('wiki/'):
        rel_str = rel_str[len('wiki/'):]
    start_line = end_line = None
    m = _WIKI_RANGE_RE.match(rel_str)
    if m:
        rel_str = m.group('path')
        start_line, end_line = int(m.group('start')), int(m.group('end'))
    try:
        p = _wiki_root() / rel_str
        if not p.exists():
            return f"(wiki 文件未找到: {rel_str})"
        text = p.read_text(encoding='utf-8', errors='ignore')
        if start_line is not None:
            lines = text.split('\n')
            excerpt = '\n'.join(lines[start_line - 1:end_line])
            return f"L{start_line}-{end_line} | {excerpt[:max_chars].strip()}"
        # Strip frontmatter
        if text.startswith('---'):
            end = text.find('---', 3)
            if end != -1:
                text = text[end + 3:].lstrip()
        return text[:max_chars].strip()
    except OSError:
        return f"(wiki 读取失败: {rel_str})"


def _get_item_text(item_id: str) -> Tuple[str, str]:
    """Resolve an item ID back to source text and source label."""
    # --- L1 built-in memory (with reference following) ---
    if item_id.startswith('MEMORY.md:') or item_id.startswith('USER.md:'):
        fname, idx_str = item_id.split(':', 1)
        try:
            idx = int(idx_str)
            mem_path = _memory_dir() / fname
            entries = mem_path.read_text(encoding='utf-8').split('\n§\n')
            if idx < len(entries):
                entry = entries[idx].strip()
                # Reference following: entry contains '→ wiki/...' → read target
                m = re.search(r'→\s*(wiki/[^\s]+\.md)', entry)
                if m:
                    summary = _read_wiki_summary(m.group(1))
                    return (f"{entry} | 详情: {summary}", 'built-in memory → obsidian wiki')
                return (entry[:150], 'built-in memory')
        except (ValueError, IndexError, OSError):
            pass
        return (item_id, 'built-in memory')

    # --- L2 session message (real content) ---
    if item_id.startswith('session:'):
        try:
            chunk_rowid = int(item_id.split(':', 1)[1])
            conn = sqlite3.connect(str(_memory_dir() / 'index.sqlite'))
            cur = conn.cursor()
            cur.execute('SELECT text FROM session_chunks WHERE rowid=?', (chunk_rowid,))
            row = cur.fetchone()
            conn.close()
            if row:
                text = (row[0] or '')[:150].strip()
                return (text, 'session history')
        except Exception:
            pass
        return (item_id, 'session history')

    # --- L2 context expansion neighbour (ReFind ±2 window) ---
    if item_id.startswith('sessionctx:'):
        try:
            chunk_rowid = int(item_id.split(':', 1)[1])
            conn = sqlite3.connect(str(_memory_dir() / 'index.sqlite'))
            cur = conn.cursor()
            cur.execute('SELECT text FROM session_chunks WHERE rowid=?', (chunk_rowid,))
            row = cur.fetchone()
            conn.close()
            if row:
                text = (row[0] or '')[:150].strip()
                return (f"[上下文扩展] {text}", 'session history')
        except Exception:
            pass
        return (item_id, 'session history')

    # --- L3 Honcho (cached paragraphs) ---
    if item_id.startswith('honcho:'):
        try:
            idx = int(item_id.split(':', 1)[1])
            if idx < len(_L3_PARAS):
                return (_L3_PARAS[idx][:150], 'honcho semantic')
        except (ValueError, IndexError):
            pass
        return (item_id, 'honcho semantic')

    # --- L4 Obsidian wiki ---
    if item_id.startswith('wiki:'):
        rel = item_id.split(':', 1)[1]
        return (_read_wiki_summary(rel), 'obsidian wiki')

    return (item_id, 'unknown')


def assemble_ledger(
    fused: List[Tuple[str, float]],
    sources: Dict[str, List[str]],
    query: str
) -> str:
    """Assemble structured evidence ledger from RRF-fused results."""
    if not fused:
        return ""

    entities = extract_entities(query)
    entity_hint = ""
    if entities:
        names = ', '.join(f"{e[0]}({e[1]})" for e in entities)
        entity_hint = f"\n实体命中：{names}"

    lines = [f"--- RRF 融合召回（{min(len(fused), 5)} 条）---{entity_hint}"]

    for rank, (item_id, score) in enumerate(fused[:5], 1):
        text, source_label = _get_item_text(item_id)

        if 'obsidian' in source_label:
            trust = '高'
        elif source_label == 'built-in memory':
            trust = '高'
        elif source_label == 'session history':
            trust = '中'
        elif source_label == 'honcho semantic':
            trust = '中'
        else:
            trust = '低'

        lines.append(
            f"[RRF #{rank} · {source_label} · 可信度: {trust} · 溯源: {_trace_ref(item_id)}]\n"
            f"  {text}"
        )

    return '\n'.join(lines)


def _trace_ref(item_id: str) -> str:
    """Render a compact drill-down handle for a fused item.

    Every ledger line carries a deterministic pointer back to ground-truth
    evidence (TencentDB Agent Memory pattern: top-layer symbol → raw source):
      L1  MEMORY.md:3          → MEMORY.md#3        (memories file, entry index)
      L2  session:12345        → msg#12345          (state.db messages.id, scrollable)
      L2+ sessionctx:12345     → msg#12345·上下文扩展 (ReFind ±2 expansion neighbour)
      L3  honcho:2             → honcho¶2           (prefetch paragraph cache index)
      L4  wiki:raw/x.md:3-9    → raw/x.md:3-9       (vault-relative path + hit lines)
    """
    if item_id.startswith(('MEMORY.md:', 'USER.md:')):
        return item_id.replace(':', '#')
    if item_id.startswith('sessionctx:'):
        return 'msg#' + item_id.split(':', 1)[1] + '·上下文扩展'
    if item_id.startswith('session:'):
        return 'msg#' + item_id.split(':', 1)[1]
    if item_id.startswith('honcho:'):
        return 'honcho¶' + item_id.split(':', 1)[1]
    if item_id.startswith('wiki:'):
        return item_id.split(':', 1)[1]
    return item_id


# ---------------------------------------------------------------------------
# L5: temporal recall channel (Hindsight-inspired: time as first-class)
# ---------------------------------------------------------------------------
_WEEKDAY_MAP = {'一': 0, '二': 1, '三': 2, '四': 3, '五': 4, '六': 5, '日': 6, '天': 6}


def _month_window(year: int, month: int) -> Tuple[float, float]:
    """Full-month window [start, end) as epoch timestamps."""
    import datetime as _dt
    start = _dt.datetime(year, month, 1)
    end = _dt.datetime(year + 1, 1, 1) if month == 12 else _dt.datetime(year, month + 1, 1)
    return start.timestamp(), end.timestamp()


def _parse_time_window(query: str):
    """Detect a Chinese time expression in the query.

    Returns (start_ts, end_ts, label, stripped_query) or None.
    stripped_query has the time span removed so keyword scoring isn't polluted.
    """
    import datetime as _dt
    now = _dt.datetime.now()
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)

    def day(d: _dt.datetime) -> Tuple[float, float]:
        s = d.replace(hour=0, minute=0, second=0, microsecond=0)
        return s.timestamp(), (s + _dt.timedelta(days=1)).timestamp()

    def this_week_monday() -> _dt.datetime:
        return today0 - _dt.timedelta(days=now.weekday())

    # Ordered rules: first match wins. Each returns (start, end, label).
    m = re.search(r'(\d{1,2})个?月前', query)
    if m:
        n = int(m.group(1))
        y, mo = now.year, now.month - n
        while mo <= 0:
            mo += 12
            y -= 1
        s, e = _month_window(y, mo)
        return s, e, f'{n}个月前', query[:m.start()] + query[m.end():]

    m = re.search(r'(\d{1,2})周前', query)
    if m:
        n = int(m.group(1))
        monday = this_week_monday() - _dt.timedelta(weeks=n)
        return monday.timestamp(), (monday + _dt.timedelta(days=7)).timestamp(), f'{n}周前', query[:m.start()] + query[m.end():]

    m = re.search(r'(\d{1,2})天前', query)
    if m:
        n = int(m.group(1))
        s, e = day(today0 - _dt.timedelta(days=n))
        return s, e, f'{n}天前', query[:m.start()] + query[m.end():]

    m = re.search(r'上上周([一二三四五六日天])?', query)
    if m:
        monday = this_week_monday() - _dt.timedelta(weeks=2)
        if m.group(1):
            d = monday + _dt.timedelta(days=_WEEKDAY_MAP[m.group(1)])
            s, e = day(d)
        else:
            s, e = monday.timestamp(), (monday + _dt.timedelta(days=7)).timestamp()
        return s, e, m.group(0), query[:m.start()] + query[m.end():]

    m = re.search(r'上周([一二三四五六日天])?', query)
    if m:
        monday = this_week_monday() - _dt.timedelta(weeks=1)
        if m.group(1):
            d = monday + _dt.timedelta(days=_WEEKDAY_MAP[m.group(1)])
            s, e = day(d)
        else:
            s, e = monday.timestamp(), (monday + _dt.timedelta(days=7)).timestamp()
        return s, e, m.group(0), query[:m.start()] + query[m.end():]

    m = re.search(r'(?:这周|本周)([一二三四五六日天])?', query)
    if m:
        monday = this_week_monday()
        if m.group(1):
            d = monday + _dt.timedelta(days=_WEEKDAY_MAP[m.group(1)])
            s, e = day(d)
        else:
            s, e = monday.timestamp(), now.timestamp()
        return s, e, m.group(0), query[:m.start()] + query[m.end():]

    m = re.search(r'上个月|上月', query)
    if m:
        y, mo = (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)
        s, e = _month_window(y, mo)
        return s, e, '上个月', query[:m.start()] + query[m.end():]

    m = re.search(r'这个月|本月', query)
    if m:
        s, _e = _month_window(now.year, now.month)
        return s, now.timestamp(), '这个月', query[:m.start()] + query[m.end():]

    m = re.search(r'(\d{1,2})月份?', query)
    if m:
        mo = int(m.group(1))
        if 1 <= mo <= 12:
            y = now.year if mo <= now.month else now.year - 1
            s, e = _month_window(y, mo)
            return s, e, f'{mo}月', query[:m.start()] + query[m.end():]

    m = re.search(r'今年', query)
    if m:
        s, _e = _month_window(now.year, 1)
        return s, now.timestamp(), '今年', query[:m.start()] + query[m.end():]

    m = re.search(r'去年', query)
    if m:
        s, e = _month_window(now.year - 1, 1)[0], _month_window(now.year, 1)[0]
        return s, e, '去年', query[:m.start()] + query[m.end():]

    m = re.search(r'今天|今日', query)
    if m:
        return today0.timestamp(), now.timestamp(), '今天', query[:m.start()] + query[m.end():]

    m = re.search(r'昨天|昨日', query)
    if m:
        s, e = day(today0 - _dt.timedelta(days=1))
        return s, e, '昨天', query[:m.start()] + query[m.end():]

    m = re.search(r'前天', query)
    if m:
        s, e = day(today0 - _dt.timedelta(days=2))
        return s, e, '前天', query[:m.start()] + query[m.end():]

    m = re.search(r'近期', query)
    if m:
        return (today0 - _dt.timedelta(days=14)).timestamp(), now.timestamp(), '近期', query[:m.start()] + query[m.end():]

    m = re.search(r'最近', query)
    if m:
        return (today0 - _dt.timedelta(days=7)).timestamp(), now.timestamp(), '最近', query[:m.start()] + query[m.end():]

    m = re.search(r'(?:星期|周)([一二三四五六日天])', query)
    if m:
        target = _WEEKDAY_MAP[m.group(1)]
        delta = (now.weekday() - target) % 7 or 7  # most recent such weekday, not today
        s, e = day(today0 - _dt.timedelta(days=delta))
        return s, e, m.group(0), query[:m.start()] + query[m.end():]

    return None


def _l5_temporal_search(query: str, top_k: int = 8) -> List[str]:
    """Time-window recall against state.db messages.

    If keyword terms remain after stripping the time expression, rank window
    messages by term occurrence; otherwise (pure time query like "6月发生了什么")
    surface the most recent messages in the window.
    """
    parsed = _parse_time_window(query)
    if not parsed:
        return []
    start_ts, end_ts, _label, stripped = parsed

    db_path = _memory_dir() / 'index.sqlite'
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        # dsh 版：chunk_meta.observed_at 为 ISO 时间戳文本，与 session_chunks 按 rowid 关联
        start_iso = datetime.fromtimestamp(start_ts).isoformat()
        end_iso = datetime.fromtimestamp(end_ts).isoformat()
        cur.execute(
            "SELECT sc.rowid, sc.text, cm.observed_at FROM session_chunks sc "
            "JOIN chunk_meta cm ON cm.rowid = sc.rowid "
            "WHERE cm.observed_at >= ? AND cm.observed_at < ? "
            "ORDER BY cm.observed_at DESC LIMIT 500",
            (start_iso, end_iso),
        )
        rows = cur.fetchall()
        conn.close()
    except Exception:
        return []
    if not rows:
        return []

    primaries, grams = _tokenize(stripped)
    hits = []
    for r in rows:
        text = (r['text'] or '').lower()
        if not text:
            continue
        score = 0
        for t in primaries:
            score += text.count(t) * 3
        for g in grams:
            score += text.count(g)
        if score > 0:
            hits.append((score, r['observed_at'], f"session:{r['rowid']}"))
    if not hits:
        return [f"session:{r['rowid']}" for r in rows[:3]]
    hits.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [h[2] for h in hits[:top_k]]


# ---------------------------------------------------------------------------
# L6: entity graph as a RECALL channel (Hindsight-inspired graph traversal)
# ---------------------------------------------------------------------------
def _l6_entity_recall(query: str, top_k: int = 8) -> List[str]:
    """Known entities actively pull related items from L1/L2/L4.

    Previously the entity graph only filtered/boosted fused results; this
    channel makes entities generate their own recall list that participates
    in RRF alongside the other channels.
    """
    entities = extract_entities(query)
    if not entities:
        return []

    hits: List[Tuple[float, str]] = []
    seen = set()

    memory_dir = _memory_dir()
    l1_entries: Dict[str, List[str]] = {}
    for fname in ['MEMORY.md', 'USER.md']:
        fpath = memory_dir / fname
        if fpath.exists():
            try:
                l1_entries[fname] = fpath.read_text(encoding='utf-8').split('\n§\n')
            except OSError:
                l1_entries[fname] = []

    db_path = _memory_dir() / 'index.sqlite'
    conn = None
    try:
        if db_path.exists():
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
    except Exception:
        conn = None

    for ent_name, _ent_type in entities:
        ent_lower = ent_name.lower()
        # L1 curated entries mentioning the entity (strongest signal)
        for fname, entries in l1_entries.items():
            for i, entry in enumerate(entries):
                c = entry.lower().count(ent_lower)
                if c:
                    item_id = f"{fname}:{i}"
                    if item_id not in seen:
                        seen.add(item_id)
                        hits.append((c * 5, item_id))
        # L2 most recent messages mentioning the entity (dsh: session_chunks)
        if conn is not None:
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT rowid, text FROM session_chunks WHERE text LIKE ? "
                    "ORDER BY rowid DESC LIMIT 3",
                    (f'%{ent_name}%',),
                )
                for rank, row in enumerate(cur.fetchall()):
                    item_id = f"session:{row['rowid']}"
                    if item_id not in seen:
                        seen.add(item_id)
                        hits.append((4 - rank, item_id))
            except Exception:
                pass
        # L4 wiki notes on the entity (reuse L4 searcher + cache)
        for item_id in _l4_wiki_search(ent_name, top_k=2):
            if item_id not in seen:
                seen.add(item_id)
                hits.append((3, item_id))

    if conn is not None:
        conn.close()
    hits.sort(key=lambda x: x[0], reverse=True)
    return [h[1] for h in hits[:top_k]]


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def rrf_fuse_prefetch(query: str, honcho_context: str = "",
                      date_from=None, date_to=None) -> str:
    """Run RRF fusion across all memory sources and return ledgdered context.

    Sources: L1 built-in memory, L2 session history, L3 Honcho, L4 Obsidian wiki,
    L5 temporal window, L6 entity graph recall.

    date_from/date_to (optional ISO date or datetime strings, e.g. '2026-10-01'
    or '2026-10-01T00:00:00') restrict the L2 session-history source to
    messages observed within [date_from, date_to]; omitted → no time filter.
    """
    if not query or not query.strip():
        return ""

    # Retrieve from all six sources
    l1_results = _l1_keyword_match(query, top_k=10)
    l2_results = _l2_session_search(query, top_k=10,
                                    date_from=date_from, date_to=date_to)
    l3_results = _l3_parse_honcho(honcho_context, top_k=10)
    l4_results = _l4_wiki_search(query, top_k=6)
    l5_results = _l5_temporal_search(query, top_k=8)
    l6_results = _l6_entity_recall(query, top_k=8)

    if not (l1_results or l2_results or l3_results or l4_results
            or l5_results or l6_results):
        return ""

    ranks = {}
    if l1_results:
        ranks['keyword'] = l1_results
    if l2_results:
        ranks['fts5'] = l2_results
    if l3_results:
        ranks['semantic'] = l3_results
    if l4_results:
        ranks['wiki'] = l4_results
    if l5_results:
        ranks['temporal'] = l5_results
    if l6_results:
        ranks['entity'] = l6_results

    # RRF fusion
    fused = rrf_fuse(ranks, k=60)

    # Entity graph filtering (boost entity-related items)
    query_entities = extract_entities(query)
    if query_entities:
        entity_names = {e[0].lower() for e in query_entities}

        def entity_boost(item):
            item_id = item[0]
            _, item_text = _get_item_text(item_id)
            item_lower = item_text.lower()
            boost = any(ename in item_lower for ename in entity_names)
            return (item[1] + 10) if boost else item[1]

        fused.sort(key=entity_boost, reverse=True)

    sources = {
        'keyword': l1_results,
        'fts5': l2_results,
        'semantic': l3_results,
        'wiki': l4_results,
        'temporal': l5_results,
        'entity': l6_results,
    }
    return assemble_ledger(fused, sources, query)
