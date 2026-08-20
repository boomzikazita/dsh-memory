#!/usr/bin/env python3
"""feishu_push.py — 飞书聊天推送（周报/告警/提醒）+ outbox 投递必达

用法:
  feishu_push.py "<text>" [chat_id]     推送（自动过 outbox：先落盘，成功标记 done）
  feishu_push.py --retry [limit]        补投所有 pending 条目（失败重投/崩溃恢复）
  feishu_push.py --status               查看 outbox 状态（pending/done 计数）

outbox（2026-08-19 借鉴 legacy system delivery_obligations，投递「必达」）：
  ~/.dsh/memories/outbox.jsonl —— 推送先入队落盘，成功才标记 done。
  fire-and-forget 的推送失败即丢失（A股日报等定时产物），outbox 让失败项
  留存，health 周检（dsh_health_report.py）扫 pending 自动补投。

凭据（优先级）:
  1. 环境变量 LARK_APP_ID / LARK_APP_SECRET
  2. ~/.env 中 LARK_APP_ID / LARK_APP_SECRET
  3. ~/.dsh/.credentials.yaml（LARK_APP_SECRET）+ settings.yaml（appId）
未配置时输出提示（非错误退出），由调用方决定是否降级。

目标 chat 默认: 环境变量 LARK_DEFAULT_CHAT（未配置则为空，由调用方传入 chat_id）

[2026-08-18 修复] post() 捕获 HTTPError 并输出飞书错误体（code/msg）。
[2026-08-19 新增] outbox 层：append→send→mark；--retry 补投；--status 查询。
"""
import sys
import os
import json
import time
import uuid
import pathlib
import urllib.request
import urllib.error

DEFAULT_CHAT = os.environ.get("LARK_DEFAULT_CHAT", "")  # 默认目标 chat_id（可经环境变量配置）
FEISHU_BASE = "https://open.feishu.cn/open-apis"
OUTBOX = pathlib.Path.home() / ".dsh" / "memories" / "outbox.jsonl"


def load_credentials():
    app_id = os.environ.get("LARK_APP_ID")
    app_secret = os.environ.get("LARK_APP_SECRET")
    # ~/.env
    env_file = pathlib.Path.home() / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("LARK_APP_ID="):
                app_id = app_id or line.split("=", 1)[1].strip().strip('"\'')
            elif line.startswith("LARK_APP_SECRET="):
                app_secret = app_secret or line.split("=", 1)[1].strip().strip('"\'')
    # ~/.dsh/.credentials.yaml（LARK_APP_SECRET，DSH 全局凭据）
    creds = pathlib.Path.home() / ".dsh" / ".credentials.yaml"
    if creds.exists() and not app_secret:
        for line in creds.read_text().splitlines():
            line = line.strip()
            if line.startswith("LARK_APP_SECRET:"):
                app_secret = line.split(":", 1)[1].strip().strip('"\'')
    # ~/.dsh/settings.yaml（lark-channel.appId）
    settings = pathlib.Path.home() / ".dsh" / "settings.yaml"
    if settings.exists() and not app_id:
        in_lark = False
        for line in settings.read_text().splitlines():
            line = line.strip()
            if line.startswith("lark-channel:"):
                in_lark = True
                continue
            if in_lark and line.startswith("appId:"):
                app_id = line.split(":", 1)[1].strip().strip('"\'')
                break
    return app_id, app_secret


def post(url, payload, headers=None):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        # 关键修复：把飞书返回的 HTTP 错误体（code/msg）带出来，便于定位
        body = ""
        try:
            body = e.read().decode()
        except Exception:
            pass
        raise RuntimeError(f"飞书 API HTTP {e.code}: {body}") from e


def send_feishu(text: str, chat_id: str) -> tuple:
    """真正推送。返回 (ok: bool, detail: str)。凭据缺失 → (False, 提示) 不抛异常。"""
    app_id, app_secret = load_credentials()
    if not app_id or not app_secret:
        return False, "未配置 LARK_APP_ID / LARK_APP_SECRET（环境变量或 ~/.env），跳过推送"
    try:
        token_resp = post(f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal",
                          {"app_id": app_id, "app_secret": app_secret})
        token = token_resp.get("tenant_access_token")
        if token_resp.get("code") != 0 or not token:
            return False, f"获取 token 失败: {token_resp}"
        msg = {"receive_id": chat_id, "msg_type": "text",
               "content": json.dumps({"text": text}, ensure_ascii=False)}
        resp = post(f"{FEISHU_BASE}/im/v1/messages?receive_id_type=chat_id", msg,
                    {"Authorization": f"Bearer {token}"})
        if resp.get("code") == 0:
            return True, f"已推送至 {chat_id} (msg_id={resp.get('data', {}).get('message_id', '?')})"
        return False, f"推送失败: code={resp.get('code')} msg={resp.get('msg')}"
    except Exception as e:
        return False, f"异常: {e}"


# ---------- outbox（投递必达：先入队，成功标记；pending 可补投） ----------
def outbox_read() -> list:
    if not OUTBOX.exists():
        return []
    out = []
    for line in OUTBOX.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def outbox_write(recs: list) -> None:
    OUTBOX.parent.mkdir(parents=True, exist_ok=True)
    OUTBOX.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + ("\n" if recs else ""), encoding="utf-8")


def outbox_append(text: str, chat_id: str) -> str:
    rec = {
        "id": f"o-{int(time.time())}-{uuid.uuid4().hex[:6]}",
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "chat": chat_id, "text": text[:4000], "status": "pending",
    }
    OUTBOX.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTBOX, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec["id"]


def outbox_mark(rec_id: str, status: str) -> None:
    recs = outbox_read()
    changed = False
    for r in recs:
        if r.get("id") == rec_id and r.get("status") != status:
            r["status"] = status
            r["done_ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            changed = True
    if changed:
        outbox_write(recs)


def outbox_retry(limit: int = 50) -> tuple:
    """补投所有 pending 条目。返回 (ok_count, fail_count)。"""
    recs = outbox_read()
    pending = [r for r in recs if r.get("status") == "pending"][:limit]
    ok = fail = 0
    for r in pending:
        ok_, detail = send_feishu(r.get("text", ""), r.get("chat") or DEFAULT_CHAT)
        if ok_:
            outbox_mark(r["id"], "done")
            ok += 1
        else:
            fail += 1
            print(f"feishu_push: 补投失败 {r['id']}: {detail}")
    if ok:
        print(f"feishu_push: 补投完成 {ok} 条成功 / {fail} 条失败（剩余 pending 继续留存）")
    return ok, fail


def outbox_status() -> None:
    recs = outbox_read()
    pending = [r for r in recs if r.get("status") == "pending"]
    done = [r for r in recs if r.get("status") == "done"]
    print(f"feishu_push outbox: 共 {len(recs)} 条（pending {len(pending)} / done {len(done)}）")
    for r in pending[:10]:
        print(f"  ⏳ {r['id']} {r['ts']} → {r['chat']}: {r['text'][:60]}")


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] == "--status":
        outbox_status()
        return
    if args and args[0] == "--retry":
        limit = int(args[1]) if len(args) > 1 and args[1].isdigit() else 50
        outbox_retry(limit)
        return
    if len(args) < 1:
        raise SystemExit("feishu_push.py <text> [chat_id] | --retry [limit] | --status")
    text = args[0]
    chat_id = args[1] if len(args) > 1 and args[1] else DEFAULT_CHAT

    # outbox 必达流程：先入队 → 尝试推送 → 成功标记 done / 失败留 pending
    rec_id = outbox_append(text, chat_id)
    ok, detail = send_feishu(text, chat_id)
    if ok:
        outbox_mark(rec_id, "done")
        print(f"feishu_push: {detail}")
    else:
        print(f"feishu_push: {detail}（已留 outbox pending={rec_id}，可 --retry 补投）")


if __name__ == "__main__":
    main()
