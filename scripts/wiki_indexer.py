#!/usr/bin/env python3
"""wiki_indexer.py — Obsidian wiki 全库检索索引（L4 通道的数据底座）

背景：dsh-memory 的 L4 原设计是"跟随 L1 规则中的 → wiki/ 指针"，但
L1 预算有限放不下全部指针，且 wiki/index.md 只收录 33/528 页——wiki 全库
实际不可检索（2026-08-19 RDP 方案检索盲区根因之一）。

本脚本：对 wiki/raw 下全部 .md 建 FTS5 索引（路径+标题+frontmatter+正文），
召回时（rrf_recall.py L4）直接查库，不占 L1 预算。

用法：
  wiki_indexer.py rebuild   全量重建索引
  wiki_indexer.py update    增量更新（mtime 变化）
  wiki_indexer.py check     索引状态与覆盖统计
"""
import re
import sqlite3
import sys
from pathlib import Path

HOME = Path.home()
WIKI_ROOT = HOME / "Nutstore Files" / "我的坚果云" / "wiki" / "raw"
DB_PATH = HOME / ".dsh" / "memories" / "wiki_index.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS wiki_docs (
    id INTEGER PRIMARY KEY,
    rel_path TEXT UNIQUE NOT NULL,      -- raw 相对路径（如 技术/工具手册/无头Linux远程桌面投射Windows.md）
    mtime REAL NOT NULL,
    title TEXT DEFAULT '',
    frontmatter TEXT DEFAULT '',         -- 完整 frontmatter（含 tags/category/source）
    first_para TEXT DEFAULT '',          -- 正文首段摘要
    body TEXT DEFAULT ''                 -- 正文（frontmatter 之后全文）
);
CREATE VIRTUAL TABLE IF NOT EXISTS wiki_fts USING fts5(
    title, frontmatter, first_para, body, rel_path UNINDEXED,
    tokenize='trigram'
);
"""


def _extract_frontmatter(text: str):
    m = re.match(r"^---\n(.*?)\n---\n?", text, re.S)
    if not m:
        return "", text
    return m.group(1), text[m.end():]


def _doc_tuple(rel: Path, full_path: Path):
    try:
        text = full_path.read_text(errors="ignore")
    except Exception:
        return None
    fm, body = _extract_frontmatter(text)
    title_m = re.search(r"^#\s+(.+)$", body, re.M)
    title = title_m.group(1).strip() if title_m else full_path.stem
    # 首段：第一个非空段（跳过标题行）
    paras = [p.strip() for p in body.split("\n\n") if p.strip() and not p.strip().startswith("#")]
    first_para = paras[0][:200] if paras else ""
    return (str(rel.as_posix()), full_path.stat().st_mtime, title, fm, first_para, body[:20000])


def connect():
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    return con


def rebuild():
    con = connect()
    # IF NOT EXISTS 不会更新已存在的表（tokenizer 变更需 DROP 重建）
    con.execute("DROP TABLE IF EXISTS wiki_fts")
    con.execute("DROP TABLE IF EXISTS wiki_docs")
    con.executescript(SCHEMA)
    n = 0
    for p in sorted(WIKI_ROOT.rglob("*.md")):
        if "_archive" in p.parts:
            continue
        t = _doc_tuple(p.relative_to(WIKI_ROOT), p)
        if not t:
            continue
        rel, mtime, title, fm, fp, body = t
        cur = con.execute(
            "INSERT INTO wiki_docs(rel_path, mtime, title, frontmatter, first_para, body) VALUES(?,?,?,?,?,?)",
            (rel, mtime, title, fm, fp, body))
        con.execute(
            "INSERT INTO wiki_fts(rowid, title, frontmatter, first_para, body, rel_path) VALUES(?,?,?,?,?,?)",
            (cur.lastrowid, title, fm, fp, body, rel))
        n += 1
    con.commit()
    print(f"rebuild: {n} 个文档已索引 → {DB_PATH}")


def update():
    con = connect()
    existing = {r[0]: r[1] for r in con.execute("SELECT rel_path, mtime FROM wiki_docs").fetchall()}
    n_new = n_upd = 0
    for p in WIKI_ROOT.rglob("*.md"):
        if "_archive" in p.parts:
            continue
        rel = str(p.relative_to(WIKI_ROOT).as_posix())
        mtime = p.stat().st_mtime
        t = _doc_tuple(p.relative_to(WIKI_ROOT), p)
        if not t:
            continue
        if rel not in existing:
            cur = con.execute(
                "INSERT INTO wiki_docs(rel_path, mtime, title, frontmatter, first_para, body) VALUES(?,?,?,?,?,?)",
                t)
            con.execute(
                "INSERT INTO wiki_fts(rowid, title, frontmatter, first_para, body, rel_path) VALUES(?,?,?,?,?,?)",
                (cur.lastrowid, t[2], t[3], t[4], t[5], t[0]))
            n_new += 1
        elif existing[rel] < mtime:
            con.execute(
                "UPDATE wiki_docs SET mtime=?, title=?, frontmatter=?, first_para=?, body=? WHERE rel_path=?",
                (t[1], t[2], t[3], t[4], t[5], rel))
            rowid = con.execute("SELECT id FROM wiki_docs WHERE rel_path=?", (rel,)).fetchone()[0]
            con.execute(
                "UPDATE wiki_fts SET title=?, frontmatter=?, first_para=?, body=? WHERE rowid=?",
                (t[2], t[3], t[4], t[5], rowid))
            n_upd += 1
    con.commit()
    print(f"update: 新增 {n_new} / 更新 {n_upd}")


def check():
    con = connect()
    n = con.execute("SELECT COUNT(*) FROM wiki_docs").fetchone()[0]
    total = sum(1 for p in WIKI_ROOT.rglob("*.md") if "_archive" not in p.parts)
    print(f"索引 {n}/{total} 文档（raw 全库）")
    # 覆盖抽查：≥3 字符 trigram 引号查询；<3 字符 LIKE 兜底（trigram 下限 3）
    for q in ["无头Linux远程桌面", "RDP", "错误事项清单", "A股"]:
        if len(q) >= 3:
            rows = con.execute(
                "SELECT rel_path FROM wiki_fts WHERE wiki_fts MATCH ? LIMIT 3",
                (f'"{q}"',)).fetchall()
        else:
            rows = con.execute(
                "SELECT rel_path FROM wiki_docs WHERE title LIKE ? OR frontmatter LIKE ? OR first_para LIKE ? LIMIT 3",
                (f"%{q}%", f"%{q}%", f"%{q}%")).fetchall()
        print(f"  查询「{q}」: {[r[0] for r in rows]}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    {"rebuild": rebuild, "update": update, "check": check}[cmd]()
