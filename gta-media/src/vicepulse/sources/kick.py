"""Kick public API (official, docs.kick.com), read-only with an app access token (client credentials).

What Kick officially offers and what VicePulse uses:
- Live status: GET https://api.kick.com/public/v1/channels?slug=... -> data[].stream.is_live, viewer_count,
  start_time (app access token is accepted).
- Webhooks (livestream.status.updated, chat.message.sent): signed with RSA PKCS#1 v1.5 + SHA-256 over
  "{Kick-Event-Message-Id}.{Kick-Event-Message-Timestamp}.{raw body}"; public key from /public/v1/public-key.
  Receiving webhooks needs a public https address, which is not set up (owner approval needed), so only the
  verification code is here.
- Video: Kick has no official clip or VOD download endpoint. Kick streamers send video the same way as YouTube
  streamers: a second OBS output to VicePulse's receiver (see feeds.py). Nothing is scraped.
"""

from __future__ import annotations

import base64
import hashlib
import time

import requests

from . import SourceError

TOKEN_URL = "https://id.kick.com/oauth/token"
API = "https://api.kick.com/public/v1"


class Kick:
    def __init__(self, client_id: str, client_secret: str, *, session: requests.Session | None = None):
        if not (client_id and client_secret):
            raise SourceError("KICK_CLIENT_ID / KICK_CLIENT_SECRET are not set")
        self.client_id, self.client_secret = client_id, client_secret
        self.http = session or requests.Session()
        self._token: str | None = None
        self._expires = 0.0

    def _auth(self) -> dict:
        if self._token is None or time.time() > self._expires - 60:
            r = self.http.post(TOKEN_URL, data={"grant_type": "client_credentials", "client_id": self.client_id,
                                                "client_secret": self.client_secret}, timeout=30)
            if r.status_code >= 400:
                raise SourceError(f"Kick token failed: HTTP {r.status_code} {r.text[:200]}")
            body = r.json()
            self._token, self._expires = body["access_token"], time.time() + float(body.get("expires_in", 3600))
        return {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}

    def channels(self, slugs: list[str]) -> list[dict]:
        """Live status for up to 50 channel slugs, normalised."""
        slugs = [s.strip().lower() for s in slugs if s.strip()][:50]
        if not slugs:
            return []
        r = self.http.get(f"{API}/channels", params=[("slug", s) for s in slugs], headers=self._auth(), timeout=30)
        if r.status_code >= 400:
            raise SourceError(f"Kick channels failed: HTTP {r.status_code} {r.text[:200]}")
        out = []
        for ch in r.json().get("data") or []:
            st = ch.get("stream") or {}
            out.append({"slug": ch.get("slug"), "broadcaster_user_id": ch.get("broadcaster_user_id"),
                        "title": ch.get("stream_title"), "category": (ch.get("category") or {}).get("name"),
                        "is_live": bool(st.get("is_live")), "viewer_count": st.get("viewer_count"),
                        "started_at": st.get("start_time"), "language": st.get("language")})
        return out

    def public_key_pem(self) -> str:
        r = self.http.get(f"{API}/public-key", timeout=30)
        if r.status_code >= 400:
            raise SourceError(f"Kick public key failed: HTTP {r.status_code}")
        data = r.json().get("data") or {}
        return data.get("public_key") or r.text


# ---------------------------------------------------------------- webhook signatures (no extra dependency)

def _der_read(buf: bytes, i: int) -> tuple[int, bytes, int]:
    tag = buf[i]
    ln = buf[i + 1]
    i += 2
    if ln & 0x80:
        n = ln & 0x7F
        ln = int.from_bytes(buf[i:i + n], "big")
        i += n
    return tag, buf[i:i + ln], i + ln


def rsa_public_key(pem: str) -> tuple[int, int]:
    """(n, e) from a PEM 'PUBLIC KEY' (SubjectPublicKeyInfo) or 'RSA PUBLIC KEY' (PKCS#1)."""
    body = "".join(line for line in pem.strip().splitlines() if not line.startswith("-----"))
    der = base64.b64decode(body)
    tag, seq, _ = _der_read(der, 0)
    if tag != 0x30:
        raise ValueError("not a DER sequence")
    t1, first, nxt = _der_read(seq, 0)
    if t1 == 0x30:                                         # SubjectPublicKeyInfo: algorithm, BIT STRING
        t2, bits, _ = _der_read(seq, nxt)
        if t2 != 0x03:
            raise ValueError("unexpected public key layout")
        _, rsa_seq, _ = _der_read(bits[1:], 0)
        t1, first, nxt = _der_read(rsa_seq, 0)
        seq = rsa_seq
    if t1 != 0x02:
        raise ValueError("unexpected RSA key layout")
    _, e_bytes, _ = _der_read(seq, nxt)
    return int.from_bytes(first, "big"), int.from_bytes(e_bytes, "big")


_SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


def verify_rsa_sha256(message: bytes, signature: bytes, n: int, e: int) -> bool:
    """RSASSA-PKCS1-v1_5 verification with SHA-256 (RFC 8017, section 8.2.2)."""
    k = (n.bit_length() + 7) // 8
    if len(signature) != k:
        return False
    s = int.from_bytes(signature, "big")
    if s >= n:
        return False
    em = pow(s, e, n).to_bytes(k, "big")
    t = _SHA256_PREFIX + hashlib.sha256(message).digest()
    if k < len(t) + 11:
        return False
    expected = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    return hashlib.sha256(em).digest() == hashlib.sha256(expected).digest() and em == expected


def verify_webhook(headers: dict, raw_body: bytes, public_key_pem: str) -> bool:
    """True if a Kick webhook request is genuinely from Kick. headers: case-insensitive names accepted."""
    h = {k.lower(): v for k, v in headers.items()}
    msg_id, ts, sig = h.get("kick-event-message-id"), h.get("kick-event-message-timestamp"), h.get("kick-event-signature")
    if not (msg_id and ts and sig):
        return False
    try:
        n, e = rsa_public_key(public_key_pem)
        signature = base64.b64decode(sig)
    except (ValueError, TypeError):
        return False
    return verify_rsa_sha256(f"{msg_id}.{ts}.".encode() + raw_body, signature, n, e)


__all__ = ["Kick", "verify_webhook", "rsa_public_key", "verify_rsa_sha256"]
