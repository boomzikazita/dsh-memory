#!/usr/bin/env python3
"""graph_query.py — memory_graph 查询引擎：实体沿边（curated 类型边 + 自动共现边）

双源沿边查询：
  curated  wiki/_meta/relations.md  —— 手工类型边（works_at/met_via/relates…，带证据）
  auto     ~/.dsh/data/relations.json —— 自动共现边（权重 + 会话证据，light_graph.py 生成）

用法:
  python3 graph_query.py "张三和李四"      # 自然语言自动提实体 → 沿边文本
  python3 graph_query.py 张三 --json        # 结构化输出（供插件工具/resource 沿边消费）
  python3 graph_query.py 张三 --min-w 5     # auto 边权重下限过滤（默认 0）

这是 resource 工具方案（统一资源视图）的沿边数据源：
  resource ls mem://person/<实体>  ← 调本引擎拿邻接
"""
import argparse
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import rrf_recall  # 复用实体表/提取（同一插件 scripts 目录）

HOME = pathlib.Path.home()
WIKI = pathlib.Path(os.environ.get("DSH_WIKI_ROOT", str(HOME / "wiki")))
CURATED = WIKI / "_meta" / "relations.md"
AUTO = HOME / ".dsh" / "data" / "relations.json"
KNOWN = [name for name, _ in rrf_recall._ALL_ENTITIES]


# ---------- curated 解析（wiki/_meta/relations.md 三元组表） ----------
def parse_curated():
    """返回 [{a, rel, b, evidence}]（a/b 保留原文；已知实体另标 names 列表）"""
    if not CURATED.exists():
        return []
    out = []
    try:
        text = CURATED.read_text(errors="ignore")
    except Exception:
        return []
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
        names_a = [e for e in KNOWN if e in a]
        names_b = [e for e in KNOWN if e in b]
        out.append({"a": a, "rel": rel, "b": b, "evidence": ev,
                    "names_a": names_a, "names_b": names_b})
    return out


# ---------- auto 加载（relations.json） ----------
def load_auto():
    if not AUTO.exists():
        return {"nodes": [], "edges": []}
    try:
        return json.loads(AUTO.read_text())
    except Exception:
        return {"nodes": [], "edges": []}


# ---------- 沿边查询 ----------
def query(entity: str, min_w: float = 0.0):
    curated = parse_curated()
    auto = load_auto()
    c_edges, a_edges = [], []
    # curated：A 或 B 含目标实体（文本级匹配，兼容非已知实体单元格）
    for e in curated:
        if entity in e["a"] or entity in e["b"]:
            c_edges.append(e)
    # auto：source/target 精确等于实体
    for e in auto.get("edges", []):
        if e["source"] == entity or e["target"] == entity:
            if e["weight"] >= min_w:
                a_edges.append(e)
    return {"entity": entity, "curated": c_edges, "auto": a_edges,
            "stats": {"curated_edges": len(c_edges), "auto_edges": len(a_edges)}}


def fmt_text(r):
    lines = [f"# memory_graph: {r['entity']} 的沿边"]
    lines.append(f"## curated 类型边（wiki/_meta/relations.md） {len(r['curated'])} 条")
    if not r["curated"]:
        lines.append("（无）")
    for e in r["curated"]:
        lines.append(f"- {e['a']} ─({e['rel']})→ {e['b']}  | 证据: {e['evidence']}")
    lines.append(f"## auto 共现边（relations.json） {len(r['auto'])} 条")
    if not r["auto"]:
        lines.append("（无）")
    for e in sorted(r["auto"], key=lambda x: -x["weight"])[:20]:
        ev = e["evidence"][0] if e["evidence"] else {}
        lines.append(f"- {e['source']} ── {e['target']}  w={e['weight']}  | 证据: {ev.get('src','?')}@{ev.get('date','?')}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query", help="实体名或含实体的自然语言")
    ap.add_argument("--min-w", type=float, default=0.0, help="auto 边权重下限")
    ap.add_argument("--json", action="store_true", help="结构化输出")
    args = ap.parse_args()

    # 实体提取：优先 query 中命中的已知实体；否则整串当实体名
    q_entities = {e for e, _ in rrf_recall.extract_entities(args.query)}
    q_entities |= {t for t in re.split(r"[\s,，。和与及]", args.query) if t in KNOWN}
    entity = sorted(q_entities, key=len, reverse=True)[0] if q_entities else args.query.strip()

    r = query(entity, args.min_w)
    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
    else:
        print(fmt_text(r))


if __name__ == "__main__":
    main()
