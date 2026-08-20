#!/usr/bin/env python3
"""vault.py — dsh-memory 敏感信息加密库（Fernet 认证加密）

用法:
  vault.py init                 # 生成密钥 ~/.dsh/.memory-vault.key（0600）
  vault.py set <ref> <value>    # 写入/更新条目（stdin 或参数）
  vault.py get <ref>            # 读取条目（仅 stdout，调用方负责不落会话）
  vault.py list                 # 列出 ref 列表（不含内容）
  vault.py del <ref>            # 删除条目
  vault.py check                # 完整性自检（可解密所有条目）

存储: ~/.dsh/memories/VAULT.md.enc — 整库 Fernet 加密的 JSON（换行分隔? 单 JSON 文档）
安全: 明文任何位置不得出现 vault 条目原文；get 结果标 SENSITIVE 由调用方约束
"""
import sys
import os
import json
import base64
import secrets
import pathlib

from cryptography.fernet import Fernet, InvalidToken

HOME = pathlib.Path.home()
MEMORIES = HOME / ".dsh" / "memories"
VAULT_FILE = MEMORIES / "VAULT.md.enc"
KEY_FILE = HOME / ".dsh" / ".memory-vault.key"
KEY_ENV = "DSH_MEMORY_VAULT_KEY"

# 兼容规格书的文件名（VAULT.md.enc 首行可放提示注释，随后是密文）
HEADER_PREFIX = "# dsh-memory vault (Fernet AES-128-CBC+HMAC-SHA256 authenticated). Do not edit.\n"


def _load_key() -> bytes:
    env = os.environ.get(KEY_ENV)
    if env:
        return env.encode()
    if KEY_FILE.exists():
        return KEY_FILE.read_bytes().strip()
    raise SystemExit(f"vault: 密钥不存在（{KEY_FILE} 或环境变量 {KEY_ENV}），先运行 init")


def _fernet() -> Fernet:
    return Fernet(_load_key())


def _read_db() -> dict:
    if not VAULT_FILE.exists():
        return {}
    raw = VAULT_FILE.read_text()
    if raw.startswith("#"):
        raw = raw.split("\n", 1)[1]
    if not raw.strip():
        return {}
    token = raw.strip().encode()
    try:
        plain = _fernet().decrypt(token)
    except InvalidToken:
        raise SystemExit("vault: 解密失败（密钥不匹配或文件损坏）")
    return json.loads(plain.decode())


def _write_db(db: dict) -> None:
    MEMORIES.mkdir(parents=True, exist_ok=True)
    token = _fernet().encrypt(json.dumps(db, ensure_ascii=False, indent=1).encode())
    # 原子写
    tmp = VAULT_FILE.with_suffix(".enc.tmp")
    tmp.write_bytes((HEADER_PREFIX + token.decode()).encode())
    os.replace(tmp, VAULT_FILE)
    os.chmod(VAULT_FILE, 0o600)


def cmd_init() -> None:
    if KEY_FILE.exists() or os.environ.get(KEY_ENV):
        print("vault: 密钥已存在，不覆盖（如需轮换请手动删除后重跑）")
        return
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    KEY_FILE.write_bytes(Fernet.generate_key())
    os.chmod(KEY_FILE, 0o600)
    print(f"vault: 密钥已生成 {KEY_FILE} (0600)。请备份到 Vaultwarden，丢失将无法解密 VAULT.md.enc")


def cmd_set(ref: str, value: str) -> None:
    if not ref or not value:
        raise SystemExit("vault: set 需要 ref 与 value")
    db = _read_db()
    db[ref] = value
    _write_db(db)
    print(f"vault: 已写入 {ref}（共 {len(db)} 条）")


def cmd_get(ref: str) -> None:
    db = _read_db()
    if ref not in db:
        raise SystemExit(f"vault: 未找到 {ref}（可用 list 查看）")
    print(db[ref])


def cmd_list() -> None:
    db = _read_db()
    for k in sorted(db):
        print(k)


def cmd_del(ref: str) -> None:
    db = _read_db()
    if ref not in db:
        raise SystemExit(f"vault: 未找到 {ref}")
    del db[ref]
    _write_db(db)
    print(f"vault: 已删除 {ref}")


def cmd_check() -> None:
    db = _read_db()
    bad = [k for k, v in db.items() if not isinstance(v, str) or not v.strip()]
    if bad:
        raise SystemExit(f"vault: {len(bad)} 条空/损坏条目: {bad}")
    print(f"vault: OK — {len(db)} 条可解密，文件 {VAULT_FILE}")


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    cmd = sys.argv[1]
    if cmd == "init":
        cmd_init()
    elif cmd == "set":
        cmd_set(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else sys.stdin.read().rstrip("\n"))
    elif cmd == "get":
        cmd_get(sys.argv[2])
    elif cmd == "list":
        cmd_list()
    elif cmd == "del":
        cmd_del(sys.argv[2])
    elif cmd == "check":
        cmd_check()
    else:
        raise SystemExit(f"vault: 未知命令 {cmd}")


if __name__ == "__main__":
    main()
