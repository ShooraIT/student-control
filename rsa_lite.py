"""Минимальный RSA-OAEP (SHA-256) на чистом Python — без сторонних пакетов.

Совместим с WebCrypto: браузер шифрует {name: "RSA-OAEP"} ключом с hash SHA-256,
здесь расшифровываем закрытым ключом, который хранится только у преподавателя.
"""
import base64
import hashlib
import secrets

HLEN = 32  # SHA-256


def _is_probable_prime(n, rounds=40):
    if n < 2:
        return False
    for p in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
        if n % p == 0:
            return n == p
    d, s = n - 1, 0
    while d % 2 == 0:
        d //= 2
        s += 1
    for _ in range(rounds):
        x = pow(secrets.randbelow(n - 3) + 2, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(s - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _prime(bits):
    while True:
        c = secrets.randbits(bits) | (1 << (bits - 1)) | (1 << (bits - 2)) | 1
        if _is_probable_prime(c):
            return c


def generate(bits=2048):
    e = 65537
    while True:
        p, q = _prime(bits // 2), _prime(bits // 2)
        phi = (p - 1) * (q - 1)
        if p != q and phi % e and (p * q).bit_length() == bits:
            return {"n": p * q, "e": e, "d": pow(e, -1, phi), "p": p, "q": q}


def _b64u(i):
    raw = i.to_bytes((i.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def public_jwk(key):
    return {"kty": "RSA", "alg": "RSA-OAEP-256", "ext": True, "key_ops": ["encrypt"],
            "n": _b64u(key["n"]), "e": _b64u(key["e"])}


def _mgf1(seed, length):
    out, c = b"", 0
    while len(out) < length:
        out += hashlib.sha256(seed + c.to_bytes(4, "big")).digest()
        c += 1
    return out[:length]


def _xor(a, b):
    return bytes(x ^ y for x, y in zip(a, b))


def _k(key):
    return (key["n"].bit_length() + 7) // 8


def max_chunk(key):
    return _k(key) - 2 * HLEN - 2


def encrypt(key, msg):
    """Только для самопроверки (в работе шифрует браузер)."""
    k = _k(key)
    lhash = hashlib.sha256(b"").digest()
    db = lhash + b"\0" * (k - len(msg) - 2 * HLEN - 2) + b"\1" + msg
    seed = secrets.token_bytes(HLEN)
    mdb = _xor(db, _mgf1(seed, k - HLEN - 1))
    mseed = _xor(seed, _mgf1(mdb, HLEN))
    m = int.from_bytes(b"\0" + mseed + mdb, "big")
    return pow(m, key["e"], key["n"]).to_bytes(k, "big")


def decrypt(key, ct):
    k = _k(key)
    if len(ct) != k:
        raise ValueError("bad length")
    c = int.from_bytes(ct, "big")
    # CRT — быстрее, чем pow(c, d, n)
    p, q, d = key["p"], key["q"], key["d"]
    m1, m2 = pow(c, d % (p - 1), p), pow(c, d % (q - 1), q)
    m = m2 + q * ((pow(q, -1, p) * (m1 - m2)) % p)
    em = m.to_bytes(k, "big")
    mseed, mdb = em[1:1 + HLEN], em[1 + HLEN:]
    seed = _xor(mseed, _mgf1(mdb, HLEN))
    db = _xor(mdb, _mgf1(seed, k - HLEN - 1))
    if em[0] != 0 or db[:HLEN] != hashlib.sha256(b"").digest():
        raise ValueError("decryption error")
    rest = db[HLEN:].lstrip(b"\0")
    if not rest or rest[0] != 1:
        raise ValueError("decryption error")
    return rest[1:]


def decrypt_message(key, text):
    """Формат сообщения: 'v1.' + base64-блоки через точку (каждый — RSA-OAEP)."""
    if not text.startswith("v1."):
        raise ValueError("unknown format")
    return b"".join(decrypt(key, base64.b64decode(part)) for part in text[3:].split("."))


def to_json(key):
    return {k: str(v) for k, v in key.items()}


def from_json(data):
    return {k: int(v) for k, v in data.items()}
