"""凭证字段加密。

所有密钥/密码/私钥落库前用 Fernet 对称加密,绝不明文存储。
主密钥来自环境变量 SECRET_ENCRYPTION_KEY。
"""
from cryptography.fernet import Fernet

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
