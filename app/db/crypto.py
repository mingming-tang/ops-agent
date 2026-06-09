"""凭证字段加密。

所有密钥/密码/私钥落库前用 Fernet 对称加密,绝不明文存储。
主密钥来自环境变量 SECRET_ENCRYPTION_KEY。
登录会话令牌也用同一把 Fernet 密钥签名(Fernet 自带时间戳,可按 TTL 过期)。
"""
import json

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


def _fernet() -> Fernet:
    key = get_settings().secret_encryption_key.encode()
    return Fernet(key)


def encrypt(plaintext: str | None) -> str | None:
    if plaintext is None or plaintext == "":
        return plaintext
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str | None) -> str | None:
    if ciphertext is None or ciphertext == "":
        return ciphertext
    return _fernet().decrypt(ciphertext.encode()).decode()


# ---- 登录会话令牌(签名 + TTL)----
def sign_token(payload: dict) -> str:
    return _fernet().encrypt(json.dumps(payload).encode()).decode()


def verify_token(token: str | None, max_age: int | None = None) -> dict | None:
    if not token:
        return None
    try:
        raw = _fernet().decrypt(token.encode(), ttl=max_age)
        return json.loads(raw)
    except (InvalidToken, ValueError):
        return None
