#!/usr/bin/env python3
# delta_assets.py — 最近变更资产清单（2026-08-20，pre-step 常驻注入用）
# 输出: scripts/skills/plugins 近 2 天 mtime 的文件（防批量迁移刷屏）+ 状态卡变更记录最近 5 条
# 定位: 重复建设常撞"最近资产"——注入轻量 delta（随变化量而非存量）;
#       全量查重走 check_assets 工具（按需调用）
import os, glob, time, re

HOME = os.path.expanduser("~")
NOW = time.time()
FILE_WINDOW = 2 * 86400  # 文件变更窗口：2 天（迁移日批量刷新会刷屏，故收窄）
out = []  # (type, name, extra)

for d, typ in [(HOME + "/.dsh/scripts", "script"), (HOME + "/.dsh/skills", "skill"), (HOME + "/.dsh/plugins", "plugin")]:
    if not os.path.isdir(d):
        continue
    for f in glob.glob(d + "/*"):
        if not os.path.isfile(f):
            continue
        try:
            if NOW - os.path.getmtime(f) <= FILE_WINDOW:
                out.append((typ, os.path.basename(f), time.strftime("%m-%d %H:%M", time.localtime(os.path.getmtime(f)))))
        except Exception:
            pass

card = os.environ.get("DSH_WIKI_ROOT", HOME + "/wiki") + "/raw/技术/系统运维/系统状态卡.md"
if os.path.exists(card):
    try:
        text = open(card, encoding="utf-8", errors="ignore").read()
        m = re.search(r"## 变更记录\n(.*)", text, re.S)
        if m:
            for line in m.group(1).splitlines():
                if line.startswith("- 2026-"):
                    mm = re.match(r"- (\d{4}-\d{2}-\d{2})：(.*)", line)
                    if mm:
                        out.append(("change", mm.group(1), mm.group(2)[:50]))
                    if len([x for x in out if x[0] == "change"]) >= 5:
                        break
    except Exception:
        pass

if out:
    print("最近变更（新建脚本/skill/插件/配置前先查重，避免重复建设；全量查重用 check_assets 工具）:")
    # 变更记录在前（权威信号），文件在后
    changes = sorted([x for x in out if x[0] == "change"], key=lambda x: x[1], reverse=True)
    files = sorted([x for x in out if x[0] != "change"], key=lambda x: x[2])
    for typ, name, extra in changes + files:
        print("  [%s] %s (%s)" % (typ, name, extra))

