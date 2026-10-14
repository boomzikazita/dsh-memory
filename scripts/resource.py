#!/usr/bin/env python3
"""dsh-resource 统一资源视图 — mem:// + wiki:// + scripts:// 只读门面（插件版）。

v0.2.0（2026-10-14，并入 dsh-memory）：
  - MEM_PLUGIN_SCRIPTS 由「跨插件探测 ~/.dsh/plugins/dsh-memory/scripts」改为同包相对路径
    （resource.py 与 rrf_recall.py / graph_query.py 同目录，少一层跨插件探测）
  - run_py 用 sys.executable 调同包脚本（禁裸 python3；继承宿主 config.python 选定的解释器）

来源合并：
  - 08-19 脚本验证版 ~/.dsh/scripts/resource.py（实体表/沿边/语义 search/scripts:// 视图）
  - 08-22 插件版（路径穿越防护、输出上限、条目标题视图、mem://person 沿边读取）
设计（Mirage"抄心智不抄架构"）：
  - 三个原语：ls（域里有什么）/ read（读引用）/ search（跨域检索）
  - 引用格式 <ns>://<path>：路径即身份，可跨对话传递
  - 只读：无写操作；专用能力不吞并（深度召回走 memory_recall 工具，加密库走 memory_vault_get）
"""
import argparse
import os
import re
import subprocess
import sys

HOME = os.path.expanduser("~")
MEM_ROOT = os.path.join(HOME, ".dsh", "memories")
WIKI_ROOT = os.path.join(HOME, "Nutstore Files", "我的坚果云", "wiki")
SCRIPTS_DIRS = [os.path.join(HOME, ".dsh", "scripts"), os.path.join(HOME, ".hermes", "scripts")]
# v0.2.0：同包相对路径（本文件即 memory 插件 scripts/ 目录内）
MEM_PLUGIN_SCRIPTS = os.path.dirname(os.path.abspath(__file__))
RRF_RECALL = os.path.join(MEM_PLUGIN_SCRIPTS, "rrf_recall.py")
GRAPH_QUERY = os.path.join(MEM_PLUGIN_SCRIPTS, "graph_query.py")

MAX_LS = 60
MAX_HITS = 20
HIT_CHARS = 160
MAX_READ_LINES = 200
MAX_READ_CHARS = 800
MAX_SEARCH_PER_DOMAIN = 8
MAX_WALK_FILES = 800

MEM_FILES = ["MEMORY.md", "USER.md", "DREAMS.md"]
SKIP_DIRS = {"_archive", ".git", ".obsidian", ".trash", "node_modules"}
SKIP_SUFFIX = (".bak", "~")


def safe_resolve(root, rel):
    """路径穿越防护：规范化后必须仍位于 root 内；返回绝对路径或 None。"""
    base = os.path.realpath(root)
    p = os.path.realpath(os.path.join(base, rel.lstrip("/")))
    if p == base or p.startswith(base + os.sep):
        return p
    return None


def parse_ref(ref):
    if not ref:
        return None, ""
    if "://" in ref:
        ns, path = ref.split("://", 1)
    else:
        ns, path = ref, ""
    return ns, path


def trunc(s, n):
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[: n - 3] + "..."


def run_py(script, args, timeout=30):
    try:
        r = subprocess.run([sys.executable, script] + args, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


# ---------- mem:// ----------

def known_entities():
    """从 rrf_recall.py 解析 _KNOWN_PERSONS/PROJECTS/TOPICS 实体表。"""
    out = {"person": [], "project": [], "topic": []}
    try:
        src = open(RRF_RECALL, errors="ignore").read()
    except OSError:
        return out
    for key, label in (("PERSONS", "person"), ("PROJECTS", "project"), ("TOPICS", "topic")):
        m = re.search(rf"_KNOWN_{key}\s*=\s*(?:frozenset\(\{{([\s\S]*?)\}}\)|set\(\{{([\s\S]*?)\}}\))", src)
        if m:
            body = m.group(1) or m.group(2) or ""
            out[label] = re.findall(r"[\"']([^\"']+)[\"']", body)
    return out


def mem_rules(limit=None):
    rules = []
    for f in ("MEMORY.md", "USER.md"):
        p = os.path.join(MEM_ROOT, f)
        if not os.path.exists(p):
            continue
        try:
            text = open(p, encoding="utf-8").read()
        except OSError:
            continue
        for ent in text.split("§"):
            ent = ent.strip()
            if ent:
                rules.append(f"{f[:-3]} | {ent[:60]}")
    return rules if limit is None else rules[:limit]


def mem_ls(path):
    ents = known_entities()
    if not path:
        lines = ["mem:// 总览（~/.dsh/memories）："]
        lines.append("实体（来自记忆实体表）：")
        for label in ("person", "project", "topic"):
            items = ents.get(label, [])
            shown = " ".join(items[:20]) if items else "（空）"
            lines.append(f"  {label}({len(items)}): {shown}")
        rules = mem_rules(10)
        lines.append(f"规则条目（共 {len(mem_rules())}，取前 10；ls mem://rule 看全部）：")
        for r in rules:
            lines.append(f"  - {r}")
        lines.append("文件：MEMORY.md / USER.md / DREAMS.md（read mem://<文件名>）")
        lines.append("VAULT.md.enc（加密库，请走 memory_vault_get）")
        lines.append("视图：mem://person | mem://project | mem://topic | mem://rule")
        return "\n".join(lines)
    if path in ("person", "project", "topic"):
        items = ents.get(path, [])
        return f"mem://{path}（{len(items)}）：" + (" ".join(items) if items else "（空）")
    if path == "rule":
        return "mem://rule：\n" + "\n".join(f"- {r}" for r in mem_rules())
    # 文件视图：ls 列条目标题
    p = safe_resolve(MEM_ROOT, path)
    if p is None or not os.path.isfile(p):
        return f"[resource] mem:// 无此条目：{path}"
    try:
        text = open(p, encoding="utf-8").read()
    except Exception as e:
        return f"[resource] 读取失败：{e}"
    entries = [seg.strip() for seg in text.split("§") if seg.strip()]
    out = [f"mem://{path}（{len(entries)} 条）："]
    for i, e in enumerate(entries, 1):
        title = re.sub(r"\s*<!--.*?-->", "", e.split("\n", 1)[0]).strip()
        out.append(f"  [{i}] {trunc(title, 100)}")
    return "\n".join(out[: MAX_LS + 1])


def mem_read(path):
    if not path:
        return "[resource] read 需要 ref，如 read mem://person/句号 或 read mem://MEMORY.md"
    # 沿边读取：mem://person/<名>
    if path.startswith("person/"):
        name = path.split("/", 1)[1].strip()
        out = [f"mem://person/{name}"]
        graph = run_py(GRAPH_QUERY, [name, "--min-w", "0"])
        out.append(graph or "（图谱无沿边）")
        ep = os.path.join(WIKI_ROOT, "entities", f"{name}.md")
        if os.path.exists(ep):
            try:
                body = re.sub(r"^---.*?---", "", open(ep, encoding="utf-8").read(), flags=re.S).strip()
                out.append(f"\nentities/{name}.md 摘要：\n{body[:MAX_READ_CHARS]}")
            except OSError:
                pass
        out.append(f"vault 指针（如存在）：vault:person/{name}")
        return "\n".join(out)
    p = safe_resolve(MEM_ROOT, path)
    if p is None or not os.path.isfile(p):
        return f"[resource] mem:// 无此条目：{path}"
    try:
        lines = open(p, encoding="utf-8").read().splitlines()
    except Exception as e:
        return f"[resource] 读取失败：{e}"
    if len(lines) > MAX_READ_LINES:
        return f"mem://{path}（共 {len(lines)} 行，只显示前 {MAX_READ_LINES}）：\n" + "\n".join(lines[:MAX_READ_LINES])
    return f"mem://{path}：\n" + "\n".join(lines)


# ---------- wiki:// ----------

def wiki_ls(path):
    target = WIKI_ROOT if not path else safe_resolve(WIKI_ROOT, path)
    if target is None or not os.path.isdir(target):
        return f"[resource] wiki:// 无此目录：{path}"
    items = sorted(os.listdir(target))
    dirs = [d + "/" for d in items if os.path.isdir(os.path.join(target, d))]
    files = [f for f in items if f.endswith(".md") and os.path.isfile(os.path.join(target, f))]
    lines = [f"wiki://{path or ''}（{len(dirs)} 目录 / {len(files)} md 文件）："]
    for d in dirs[:MAX_LS]:
        lines.append(f"  {d}")
    for f in files[:MAX_LS]:
        try:
            first = open(os.path.join(target, f), encoding="utf-8").readline().lstrip("#").strip()
        except OSError:
            first = ""
        lines.append(f"  {f}  ·  {trunc(first, 60)}")
    if len(dirs) + len(files) > MAX_LS:
        lines.append(f"  …（其余 {len(dirs) + len(files) - MAX_LS} 项未列，用 filter 过滤）")
    return "\n".join(lines)


def wiki_read(path):
    if not path:
        return "[resource] read 需要 ref，如 read wiki://concepts/xxx"
    p = safe_resolve(WIKI_ROOT, path)
    if p is None or (not os.path.isfile(p)):
        # 允许省略 .md 后缀：在 wiki 根内 glob 一次
        cand = None
        if p and os.path.isdir(os.path.dirname(p)):
            for f in os.listdir(os.path.dirname(p)):
                if f == os.path.basename(p) + ".md":
                    cand = os.path.join(os.path.dirname(p), f)
                    break
        if cand is None:
            return f"[resource] wiki:// 无此文件：{path}"
        p = cand
    try:
        lines = open(p, encoding="utf-8").read().splitlines()
    except Exception as e:
        return f"[resource] 读取失败：{e}"
    if len(lines) > MAX_READ_LINES:
        return f"wiki://{path}（共 {len(lines)} 行，只显示前 {MAX_READ_LINES}）：\n" + "\n".join(lines[:MAX_READ_LINES])
    return f"wiki://{path}：\n" + "\n".join(lines)


# ---------- scripts:// ----------

def scripts_ls():
    lines = ["scripts:// 脚本清单："]
    for d in SCRIPTS_DIRS:
        if not os.path.isdir(d):
            continue
        files = sorted(f for f in os.listdir(d) if f.endswith(".py") and not f.startswith("__"))
        lines.append(f"{d}（{len(files)}）：")
        lines.extend(f"  - {f}" for f in files[:MAX_LS])
    return "\n".join(lines)


# ---------- search ----------

def grep_hits(root, query, ns):
    hits, scanned = [], 0
    rx = re.compile(re.escape(query), re.IGNORECASE)
    for r, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f.endswith(SKIP_SUFFIX) or not f.endswith((".md", ".jsonl")):
                continue
            p = os.path.join(r, f)
            rel = os.path.relpath(p, root)
            try:
                for i, line in enumerate(open(p, encoding="utf-8", errors="ignore"), 1):
                    if rx.search(line):
                        hits.append((f"{ns}://{rel}", i, line))
                        if len(hits) >= MAX_HITS:
                            return hits
            except OSError:
                continue
            scanned += 1
            if scanned > MAX_WALK_FILES:
                return hits
    return hits


def search(query):
    out = [f"search: {query}"]
    # mem 域：语义召回（rrf_recall）+ grep 兜底
    mem_out = run_py(RRF_RECALL, [query, "--topk", "4", "--budget", "600"])
    out.append("\n## mem 域（memory_recall 语义）\n" + (mem_out[:1200] if mem_out else "（无）"))
    mh = grep_hits(MEM_ROOT, query, "mem")
    if mh:
        out.append("\n## mem 域（grep 补充）\n" + format_hits_lines(mh))
    # wiki 域：grep
    wh = grep_hits(WIKI_ROOT, query, "wiki")
    out.append(f"\n## wiki 域（{len(wh)} 命中，取前 {MAX_SEARCH_PER_DOMAIN}）")
    out.append(format_hits_lines(wh[:MAX_SEARCH_PER_DOMAIN]) if wh else "  （无）")
    # 图谱域：沿边
    g = run_py(GRAPH_QUERY, [query, "--min-w", "4"])
    if g:
        out.append("\n## 图谱域（沿边）\n" + g[:1200])
    return "\n".join(out)


def format_hits_lines(hits):
    return "\n".join(f"  {ref}:{lineno}  {trunc(line, HIT_CHARS)}" for ref, lineno, line in hits)


# ---------- 主入口 ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verb", required=True, choices=["ls", "read", "search"])
    ap.add_argument("--ref", default="")
    ap.add_argument("--query", default="")
    ap.add_argument("--filter", default="")
    a = ap.parse_args()

    if a.verb == "search":
        if not a.query:
            print("[resource] search 需要 query")
            return
        print(search(a.query))
        return

    ns, path = parse_ref(a.ref)
    if ns not in ("mem", "wiki", "scripts", "siyuan"):
        print("[resource] ref 需为 mem:// | wiki:// | scripts:// | siyuan://（siyuan P2 未接入）")
        return

    if ns == "mem":
        result = mem_ls(path) if a.verb == "ls" else mem_read(path)
    elif ns == "wiki":
        result = wiki_ls(path) if a.verb == "ls" else wiki_read(path)
    elif ns == "scripts":
        result = scripts_ls() if a.verb == "ls" else "[resource] scripts:// 仅支持 ls（P2 加 read）"
    else:
        result = "[resource] siyuan:// P2 接入思源 API（见 siyuan-note skill）"

    if a.filter and a.verb == "ls":
        result = "\n".join(
            [l for l in result.splitlines() if a.filter.lower() in l.lower()]
        ) or f"[resource] 过滤后无结果：{a.filter}"
    print(result)


if __name__ == "__main__":
    main()
