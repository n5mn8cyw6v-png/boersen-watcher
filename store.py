"""Verschlüsselter Speicher (AES-GCM, Schlüssel aus Passwort per PBKDF2).

Gleiches Format liest die Übersichtsseite im Browser (WebCrypto).
"""

import base64
import json
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

ITERATIONS = 250_000


def _key(password, salt):
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=ITERATIONS)
    return kdf.derive(password.encode("utf-8"))


def encrypt(obj, password):
    salt, iv = os.urandom(16), os.urandom(12)
    ct = AESGCM(_key(password, salt)).encrypt(iv, json.dumps(obj, ensure_ascii=False).encode("utf-8"), None)
    b64 = lambda b: base64.b64encode(b).decode()
    return {"v": 1, "iter": ITERATIONS, "salt": b64(salt), "iv": b64(iv), "ct": b64(ct)}


def decrypt(blob, password):
    d = lambda s: base64.b64decode(s)
    salt = d(blob["salt"])
    plain = AESGCM(_key(password, salt)).decrypt(d(blob["iv"]), d(blob["ct"]), None)
    return json.loads(plain)
