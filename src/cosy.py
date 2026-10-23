"""COSY 签名：把凭证打包成 Qoder 网关接受的 Authorization 头。

协议来自官方扩展 pi-provider-qoder 0.4.6 与 qoder2api-hub 的交叉验证，
两处 RSA 公钥逐字符一致。

流程：
  1. 随机 16 位 AES key，AES-128-CBC 加密用户信息
  2. RSA(PKCS#1 v1.5) 加密 AES key
  3. payload_b64 = base64(JSON)，sig = md5(payload_b64 \n cosy_key \n date \n body \n path)
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import uuid

# ---------------------------------------------------------------------------
# Qoder 自定义 Base64 变体
# ---------------------------------------------------------------------------

CUSTOM_ALPHABET = "_doRTgHZBKcGVjlvpC,@aFSx#DPuNJme&i*MzLOEn)sUrthbf%Y^w.(kIQyXqWA!"
STD_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
PAD = "$"

_STD2CUSTOM = {ord(STD_ALPHABET[i]): CUSTOM_ALPHABET[i] for i in range(64)}
_STD2CUSTOM[ord("=")] = PAD


def qoder_encode(plain: bytes) -> str:
    """把明文编码成 Qoder 自定义 Base64（出站请求体格式）。

    标准 base64 后按 a = n//3 切三段，重排为 尾 + 中 + 首。
    """
    std = base64.b64encode(plain).decode("ascii")
    n = len(std)
    a = n // 3
    rearranged = std[n - a:] + std[a:n - a] + std[:a]
    return rearranged.translate(_STD2CUSTOM)


# ---------------------------------------------------------------------------
# AES-128-CBC 加密（仅加密方向，够用）
# ---------------------------------------------------------------------------

def _gmul(a: int, b: int) -> int:
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        hi = a & 0x80
        a = (a << 1) & 0xFF
        if hi:
            a ^= 0x1B
        b >>= 1
    return p


def _build_sbox() -> list:
    sbox = []
    for i in range(256):
        inv = 0
        if i:
            for x in range(1, 256):
                if _gmul(i, x) == 1:
                    inv = x
                    break
        sbox.append(
            inv ^ ((inv << 1) | (inv >> 7)) & 0xFF
            ^ ((inv << 2) | (inv >> 6)) & 0xFF
            ^ ((inv << 3) | (inv >> 5)) & 0xFF
            ^ ((inv << 4) | (inv >> 4)) & 0xFF
            ^ 0x63
        )
    return sbox


_SBOX = _build_sbox()
assert _SBOX[0x00] == 0x63 and _SBOX[0x53] == 0xED, "AES S-box self-check failed"

_RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36]


def _expand_key(key: bytes) -> list:
    if len(key) != 16:
        raise ValueError("AES key must be 16 bytes, got %d" % len(key))
    w = [list(key[i:i + 4]) for i in range(0, 16, 4)]
    for i in range(4, 44):
        t = list(w[i - 1])
        if i % 4 == 0:
            t = t[1:] + t[:1]
            t = [_SBOX[b] for b in t]
            t[0] ^= _RCON[i // 4 - 1]
        w.append([w[i - 4][j] ^ t[j] for j in range(4)])
    round_keys = []
    for r in range(11):
        rk = []
        for c in range(4):
            rk.extend(w[r * 4 + c])
        round_keys.append(rk)
    return round_keys


def _encrypt_block(block: bytes, round_keys: list) -> bytes:
    state = [block[i] ^ round_keys[0][i] for i in range(16)]

    def sub_shift(s):
        out = [0] * 16
        for c in range(4):
            for r in range(4):
                out[r + 4 * c] = _SBOX[s[r + 4 * ((c + r) % 4)]]
        return out

    def mix_col(s):
        out = [0] * 16
        for c in range(4):
            a = s[4 * c:4 * c + 4]
            out[4 * c + 0] = _gmul(a[0], 2) ^ _gmul(a[1], 3) ^ a[2] ^ a[3]
            out[4 * c + 1] = a[0] ^ _gmul(a[1], 2) ^ _gmul(a[2], 3) ^ a[3]
            out[4 * c + 2] = a[0] ^ a[1] ^ _gmul(a[2], 2) ^ _gmul(a[3], 3)
            out[4 * c + 3] = _gmul(a[0], 3) ^ a[1] ^ a[2] ^ _gmul(a[3], 2)
        return out

    for rnd in range(1, 10):
        state = sub_shift(state)
        state = mix_col(state)
        state = [state[i] ^ round_keys[rnd][i] for i in range(16)]
    state = sub_shift(state)
    return bytes(state[i] ^ round_keys[10][i] for i in range(16))


def aes_cbc_encrypt(plaintext: bytes, key: bytes) -> bytes:
    """AES-128-CBC + PKCS#7 填充。key 同时作为 IV（协议要求 key == iv）"""
    round_keys = _expand_key(key)
    pad_len = 16 - (len(plaintext) % 16)
    data = plaintext + bytes([pad_len] * pad_len)
    out = b""
    prev = key
    for i in range(0, len(data), 16):
        block = bytes(data[i + j] ^ prev[j] for j in range(16))
        prev = _encrypt_block(block, round_keys)
        out += prev
    return out


# ---------------------------------------------------------------------------
# RSA PKCS#1 v1.5 加密（1024 位公钥）
# ---------------------------------------------------------------------------

SERVER_PUB_PEM = """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDA8iMH5c02LilrsERw9t6Pv5Nc
4k6Pz1EaDicBMpdpxKduSZu5OANqUq8er4GM95omAGIOPOh+Nx0spthYA2BqGz+l
6HRkPJ7S236FZz73In/KVuLnwI8JJ2CbuJap8kvheCCZpmAWpb/cPx/3Vr/J6I17
XcW+ML9FoCI6AOvOzwIDAQAB
-----END PUBLIC KEY-----"""


def _pem_to_der(pem: str) -> bytes:
    body = "".join(line for line in pem.splitlines() if not line.startswith("-----"))
    return base64.b64decode(body)


def _read_tlv(data: bytes, offset: int):
    tag = data[offset]
    length = data[offset + 1]
    offset += 2
    if length & 0x80:
        nbytes = length & 0x7F
        length = int.from_bytes(data[offset:offset + nbytes], "big")
        offset += nbytes
    return tag, data[offset:offset + length], offset + length


def _parse_rsa_pub(pem: str):
    der = _pem_to_der(pem)
    _, spki, _ = _read_tlv(der, 0)          # SubjectPublicKeyInfo
    _, _, pos = _read_tlv(spki, 0)          # AlgorithmIdentifier
    _, bits, _ = _read_tlv(spki, pos)       # BIT STRING
    _, rsaseq, _ = _read_tlv(bits, 1)       # 跳过 unused-bits 字节
    _, n_bytes, p2 = _read_tlv(rsaseq, 0)
    _, e_bytes, _ = _read_tlv(rsaseq, p2)
    return int.from_bytes(n_bytes, "big"), int.from_bytes(e_bytes, "big")


_RSA_N, _RSA_E = _parse_rsa_pub(SERVER_PUB_PEM)


def rsa_encrypt(plaintext: bytes) -> bytes:
    """PKCS#1 v1.5 type 2（随机填充）加密，返回 128 字节密文。"""
    k = (_RSA_N.bit_length() + 7) // 8
    ps_len = k - 3 - len(plaintext)
    if ps_len < 8:
        raise ValueError("plaintext too long for RSA key")
    ps = bytes(secrets.randbelow(255) + 1 for _ in range(ps_len))
    em = b"\x00\x02" + ps + b"\x00" + plaintext
    c = pow(int.from_bytes(em, "big"), _RSA_E, _RSA_N)
    return c.to_bytes(k, "big")


# ---------------------------------------------------------------------------
# COSY 头构造
# ---------------------------------------------------------------------------

COSY_VERSION = os.environ.get("Q2A_COSY_VERSION", "1.1.75")
COSY_CLIENT_TYPE = "5"
COSY_USER_AGENT = "undici"
COSY_DATA_POLICY = "agree"


def _sig_path(raw_url: str) -> str:
    from urllib.parse import urlparse
    path = urlparse(raw_url).path or "/"
    if path.startswith("/algo"):
        path = path[len("/algo"):]
    return path


def build_cosy_headers(
    *,
    uid: str,
    access_token: str,
    nickname: str = "",
    email: str = "",
    machine_id: str,
    machine_token: str,
    machine_type: str = "5",
    machine_hostname: str = "",
    machine_os: str = "",
    body: bytes,
    url: str,
    model_key: str = "",
) -> dict:
    """一次推理或模型请求所需的完整签名头。"""
    if not uid:
        raise ValueError("cosy: uid is empty")
    if not access_token:
        raise ValueError("cosy: access token is empty")

    aes_key = uuid.uuid4().hex[:16]
    info = {
        "uid": uid,
        "security_oauth_token": access_token,
        "name": nickname,
        "aid": "",
        "email": email,
    }
    info_b64 = base64.b64encode(
        aes_cbc_encrypt(json.dumps(info, separators=(",", ":")).encode(), aes_key.encode())
    ).decode("ascii")
    cosy_key = base64.b64encode(rsa_encrypt(aes_key.encode())).decode("ascii")

    payload = {
        "cosyVersion": COSY_VERSION,
        "ideVersion": "",
        "info": info_b64,
        "requestId": str(uuid.uuid4()),
        "version": "v1",
    }
    payload_b64 = base64.b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).decode("ascii")

    date = str(int(time.time()))
    raw = "\n".join([payload_b64, cosy_key, date, body.decode("latin-1"), _sig_path(url)])
    sig = hashlib.md5(raw.encode("latin-1")).hexdigest()

    headers = {
        "content-type": "application/json",
        "accept": "text/event-stream",
        "accept-encoding": "identity",
        "cache-control": "no-cache",
        "authorization": f"Bearer COSY.{payload_b64}.{sig}",
        "cosy-key": cosy_key,
        "cosy-user": uid,
        "cosy-date": date,
        "cosy-version": COSY_VERSION,
        "cosy-clienttype": COSY_CLIENT_TYPE,
        "cosy-machineid": machine_id,
        "cosy-machinetoken": machine_token,
        "cosy-machinetype": machine_type,
        "cosy-clientip": "169.254.198.161",
        "cosy-bodyhash": hashlib.md5(body).hexdigest(),
        "cosy-bodylength": str(len(body)),
        "cosy-sigpath": _sig_path(url),
        "cosy-data-policy": COSY_DATA_POLICY,
        "cosy-business-product": "cli",
        "cosy-business-type": "agent",
        "cosy-scene": "assistant",
        "login-version": "v2",
        "user-agent": COSY_USER_AGENT,
        "x-request-id": uuid.uuid4().hex,
    }
    if machine_os:
        headers["cosy-machineos"] = machine_os
        if machine_hostname:
            headers["cosy-machinehostname"] = machine_hostname
    if model_key:
        headers["x-model-key"] = model_key
        headers["x-model-source"] = "system"
    return headers


# ---------------------------------------------------------------------------
# 设备指纹：按 uid 稳定派生，跨账号互不相同
# ---------------------------------------------------------------------------

def derive_machine_id(uid: str, salt: str = "qoder2api:mid") -> str:
    """派生带连字符的 UUIDv4 形状，与官方客户端 machineId 同形。"""
    h = hashlib.md5(f"{salt}:{uid}".encode()).hexdigest()
    return f"{h[0:8]}-{h[8:12]}-4{h[13:16]}-a{h[17:20]}-{h[20:32]}"


def derive_machine_token(uid: str, salt: str = "qoder2api:mtok") -> str:
    return hashlib.md5(f"{salt}:{uid}".encode()).hexdigest()


def derive_hostname(uid: str) -> str:
    return "DESKTOP-" + hashlib.md5(f"hostname:{uid}".encode()).hexdigest()[:7].upper()


def detect_machine_os() -> str:
    """官方同款 {arch}_{platform} 形态。"""
    import platform
    import sys
    arch = (platform.machine() or "").strip().lower()
    norm = "aarch64" if arch in ("arm64", "aarch64") else "x86_64"
    if sys.platform.startswith("win"):
        plat = "win32"
    elif sys.platform == "darwin":
        plat = "darwin"
    else:
        plat = "linux"
    return f"{norm}_{plat}"
