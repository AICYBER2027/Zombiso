"""Kick official API client (fakes only) and webhook signature verification with a key built in the test."""

import base64
import hashlib

import pytest

from vicepulse.sources import SourceError
from vicepulse.sources.kick import Kick, _SHA256_PREFIX, rsa_public_key, verify_webhook

from test_clients import FakeResponse, FakeSession

# Two known primes (Mersenne 2^521-1 and 2^607-1): a 1128-bit test key, enough room for SHA-256 padding.
P, Q = 2 ** 521 - 1, 2 ** 607 - 1
N, E = P * Q, 65537
D = pow(E, -1, (P - 1) * (Q - 1))


def _der(tag, body):
    n = len(body)
    ln = bytes([n]) if n < 128 else bytes([0x80 | ((n.bit_length() + 7) // 8)]) + n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([tag]) + ln + body


def _int(v):
    b = v.to_bytes((v.bit_length() + 8) // 8, "big")
    return _der(0x02, b)


def _pem():
    rsa = _der(0x30, _int(N) + _int(E))
    alg = _der(0x30, _der(0x06, bytes.fromhex("2a864886f70d010101")) + b"\x05\x00")
    spki = _der(0x30, alg + _der(0x03, b"\x00" + rsa))
    b64 = base64.b64encode(spki).decode()
    return "-----BEGIN PUBLIC KEY-----\n" + "\n".join(b64[i:i + 64] for i in range(0, len(b64), 64)) + "\n-----END PUBLIC KEY-----"


def _sign(msg: bytes) -> str:
    k = (N.bit_length() + 7) // 8
    t = _SHA256_PREFIX + hashlib.sha256(msg).digest()
    em = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    return base64.b64encode(pow(int.from_bytes(em, "big"), D, N).to_bytes(k, "big")).decode()


def test_webhook_signature_is_verified():
    pem = _pem()
    assert rsa_public_key(pem) == (N, E)
    body = b'{"broadcaster":{"channel_slug":"x"},"is_live":true}'
    h = {"Kick-Event-Message-Id": "01J000", "Kick-Event-Message-Timestamp": "2026-11-18T01:00:00Z"}
    h["Kick-Event-Signature"] = _sign(b"01J000.2026-11-18T01:00:00Z." + body)
    assert verify_webhook(h, body, pem)
    assert not verify_webhook(h, body + b" ", pem)                                  # body changed
    assert not verify_webhook({**h, "Kick-Event-Message-Id": "01J001"}, body, pem)  # replayed under another id
    assert not verify_webhook({**h, "Kick-Event-Signature": "bm90IGEgc2lnbmF0dXJl"}, body, pem)
    assert not verify_webhook({}, body, pem)


def test_live_status_from_the_official_channels_endpoint():
    http = FakeSession([
        ("POST", "id.kick.com/oauth/token", FakeResponse(200, {"access_token": "APP", "expires_in": 3600})),
        ("GET", "/public/v1/channels", FakeResponse(200, {"data": [
            {"slug": "gtaguy", "broadcaster_user_id": 7, "stream_title": "Heists", "category": {"name": "GTA V"},
             "stream": {"is_live": True, "viewer_count": 1234, "start_time": "2026-11-18T00:00:00Z", "language": "en"}},
            {"slug": "offline", "broadcaster_user_id": 8, "stream": {"is_live": False}}]})),
    ])
    out = Kick("id", "secret", session=http).channels(["GTAguy", "offline", " "])
    assert out[0] == {"slug": "gtaguy", "broadcaster_user_id": 7, "title": "Heists", "category": "GTA V",
                      "is_live": True, "viewer_count": 1234, "started_at": "2026-11-18T00:00:00Z", "language": "en"}
    assert out[1]["is_live"] is False
    call = http.calls[1]
    assert call[2]["params"] == [("slug", "gtaguy"), ("slug", "offline")]
    assert call[2]["headers"]["Authorization"] == "Bearer APP"
    with pytest.raises(SourceError):
        Kick("", "")
