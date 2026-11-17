"""Offline tests: no call leaves the machine (Telegram and Meta calls are replaced)."""
import asyncio
import hashlib
import hmac
import json
import os
import time

import pytest

os.environ.update({
    "TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "42", "META_ACCESS_TOKEN": "tok",
    "META_PHONE_NUMBER_ID": "555", "META_APP_SECRET": "shh", "META_VERIFY_TOKEN": "vt",
})

import app as bridge  # noqa: E402
import store  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def calls(tmp_path, monkeypatch):
    object.__setattr__(bridge.settings, "DB_PATH", str(tmp_path / "t.db"))
    log = {"tg": [], "wa": [], "tpl": []}
    counter = iter(range(100, 1000))

    async def fake_say(text, reply_to=None):
        log["tg"].append(text)
        return {"message_id": next(counter)}

    async def fake_wa(to, text):
        log["wa"].append((to, text))
        return {}

    async def fake_tpl(to, name, lang="en_US"):
        log["tpl"].append((to, name, lang))
        return {}

    async def noop(*a, **k):
        return None

    async def idle():
        await asyncio.Event().wait()

    monkeypatch.setattr(bridge, "tg_say", fake_say)
    monkeypatch.setattr(bridge, "wa_send_text", fake_wa)
    monkeypatch.setattr(bridge, "wa_send_template", fake_tpl)
    monkeypatch.setattr(bridge, "wa_mark_read", noop)
    monkeypatch.setattr(bridge, "tg_ack", noop)
    monkeypatch.setattr(bridge, "telegram_loop", idle)
    return log


def signed(body: dict) -> tuple[bytes, dict]:
    raw = json.dumps(body).encode()
    sig = "sha256=" + hmac.new(b"shh", raw, hashlib.sha256).hexdigest()
    return raw, {"X-Hub-Signature-256": sig, "Content-Type": "application/json"}


def inbound(msg_id="wamid.1", text="Hi, is the shop open?"):
    return {"entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": "555"},
        "contacts": [{"wa_id": "919876543210", "profile": {"name": "Ravi"}}],
        "messages": [{"id": msg_id, "from": "919876543210", "type": "text", "text": {"body": text}}],
    }}]}]}


def test_verify_handshake(calls):
    with TestClient(bridge.app) as c:
        ok = c.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "vt", "hub.challenge": "777"})
        assert ok.status_code == 200 and ok.text == "777"
        assert c.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "no", "hub.challenge": "1"}).status_code == 403


def test_bad_signature_rejected(calls):
    with TestClient(bridge.app) as c:
        raw, h = signed(inbound())
        h["X-Hub-Signature-256"] = "sha256=" + "0" * 64
        assert c.post("/webhook", content=raw, headers=h).status_code == 401
    assert calls["tg"] == []


def test_forward_reply_and_dedupe(calls):
    with TestClient(bridge.app) as c:
        raw, h = signed(inbound())
        assert c.post("/webhook", content=raw, headers=h).status_code == 200
        assert c.post("/webhook", content=raw, headers=h).status_code == 200   # Meta retry
        c.portal.call(asyncio.sleep, 0.05)
        assert calls["tg"] == ["💬 Ravi (+919876543210)\nHi, is the shop open?"]
        # owner replies to the forwarded Telegram message (id 100)
        c.portal.call(bridge.on_telegram, {"message_id": 7, "text": "Yes, till 10 pm", "reply_to_message": {"message_id": 100}})
        assert calls["wa"] == [("919876543210", "Yes, till 10 pm")]


def test_outside_24h_needs_template(calls):
    with TestClient(bridge.app) as c:
        store.touch_contact("919000000001", "Old")
        store._run("UPDATE contacts SET last_inbound=? WHERE wa_id=?", (time.time() - 25 * 3600, "919000000001"))
        c.portal.call(bridge.on_telegram, {"message_id": 8, "text": "/to +91 90000 00001 hello"})
        assert calls["wa"] == [] and "template" in calls["tg"][-1]
        c.portal.call(bridge.on_telegram, {"message_id": 9, "text": "/template 919000000001 hello_world"})
        assert calls["tpl"] == [("919000000001", "hello_world", "en_US")]
