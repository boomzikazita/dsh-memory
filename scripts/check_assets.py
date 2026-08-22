#!/usr/bin/env python3
# check_assets.py — 按需资产查重（2026-08-20 实验版）
# 用法: python3 check_assets.py <关键词...> [--json]
# 检索: ~/.dsh/scripts / skills / plugins / 状态卡变更记录 / automations
# 定位: 解决"重复建设"——建文件/配置前查是否已有相近资产（软规则失效的硬替代）
import sys, os, json, re, glob

HOME = os.path.expanduser("~")
args = [a for a in sys.argv[1:] if not a.startswith("--")]
if not args:
    print("用法: check_assets.py <关键词...> [--json]")
    sys.exit(1)

hits = []  # (type, name, desc)

def match(text, keywords):
    return any(k.lower() in text.lower() for k in keywords)

# 1. scripts：文件名 + 文件头注释（首条 # 非 shebang 行）
for f in sorted(glob.glob(HOME + "/.dsh/scripts/*.sh") + glob.glob(HOME + "/.dsh/scripts/*.py")):
    name = os.path.basename(f)
    desc = ""
    try:
        with open(f, encoding="utf-8", errors="ignore") as fh:
            for line in fh.readlines()[:10]:
                line = line.strip()
                if line.startswith("#") and not line.startswith("#!"):
                    cand = line.lstrip("# ").strip()
                    if cand.startswith(name):  # 注释常以文件名开头，剥离避免重复
                        cand = cand[len(name):].lstrip(" —-:：").strip()
                    desc = cand
                    break
    except Exception:
        pass
    if match(name + " " + desc, args):
        hits.append(("script", name, desc))

# 2. skills：目录名 + SKILL.md description
for d in sorted(glob.glob(HOME + "/.dsh/skills/*")):
    if not os.path.isdir(d):
        continue
    name = os.path.basename(d)
    desc = ""
    sfile = os.path.join(d, "SKILL.md")
    if os.path.exists(sfile):
        try:
            for line in open(sfile, encoding="utf-8", errors="ignore").read().splitlines()[:15]:
                if line.startswith("description"):
                    desc = line.split(":", 1)[-1].strip()[:90]
                    break
        except Exception:
            pass
    if match(name + " " + desc, args):
        hits.append(("skill", name, desc))

# 3. plugins：目录名 + package.json description
for d in sorted(glob.glob(HOME + "/.dsh/plugins/*")):
    name = os.path.basename(d)
    if name.startswith("."):
        continue
    desc = ""
    pj = os.path.join(d, "package.json")
    if os.path.exists(pj):
        try:
            desc = json.load(open(pj)).get("description", "")[:90]
        except Exception:
            pass
    if match(name + " " + desc, args):
        hits.append(("plugin", name, desc))

# 4. 状态卡变更记录（最近 30 条，每条含日期+动作）
card = HOME + "/Nutstore Files/我的坚果云/wiki/raw/技术/系统运维/系统状态卡.md"
if os.path.exists(card):
    try:
        text = open(card, encoding="utf-8", errors="ignore").read()
        m = re.search(r"## 变更记录\n(.*)", text, re.S)
        if m:
            for line in m.group(1).splitlines()[:30]:
                if line.startswith("- 2026-"):
                    if match(line, args):
                        hits.append(("change-log", line[:130], ""))
    except Exception:
        pass

# 5. automations：dsh_automation.json 定义名
try:
    ad = json.load(open(HOME + "/.dsh/storages/dsh_automation.json"))
    defs = ad.get("tables", {}).get("definitions", {}) or {}
    for name in defs:
        if match(name, args):
            hits.append(("automation", name, ""))
except Exception:
    pass

if "--json" in sys.argv:
    print(json.dumps([{"type": t, "name": n, "desc": d} for t, n, d in hits], ensure_ascii=False, indent=1))
else:
    if hits:
        print("⚠ 已有相近资产（check_assets）:")
        for t, n, d in hits:
            print("  [%s] %s%s" % (t, n, (" — " + d) if d else ""))
        print("→ 新建前先看这些是否已覆盖需求；重复则扩展而非重建")
    else:
        print("✓ 未发现相近资产，可安全新建。")
