"""WhatsApp (Meta Cloud API) <-> Telegram bridge.

Customer writes on WhatsApp  -> Meta calls POST /webhook -> message is forwarded to your Telegram chat.
You reply (Telegram "Reply") -> the bot sends it back to that customer on WhatsApp.

Run with ONE worker (the Telegram poller lives inside the app):
    uvicorn app:app --host 0.0.0.0 --port 8000
"""
import asyncio
import hashlib
import hmac
import json
import logging
import re
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request, Response

import store
from config import settings

log = logging.getLogger("bridge")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

GRAPH = f"https://graph.facebook.com/{settings.GRAPH_VERSION}"
TG = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}"
WINDOW_S = 24 * 3600          # WhatsApp allows free text only within 24 h of the customer's last message
HELP = (
    "Reply to a forwarded WhatsApp message to answer that customer.\n\n"
    "/to <number> <text>  write to a number (only inside the 24 h window)\n"
    "/template <number> <template_name> [lang]  start a chat with an approved template (default lang en_US)\n"
    "/help  this text"
)

http: httpx.AsyncClient | None = None
_tasks: set[asyncio.Task] = set()


class GraphError(Exception):
    pass


# ---------------- Telegram ----------------

async def tg(method: str, **params):
    r = await http.post(f"{TG}/{method}", json=params, timeout=45)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method}: {data.get('description')}")
    return data["result"]


async def tg_say(text: str, reply_to: int | None = None) -> dict:
    params = {"chat_id": settings.TELEGRAM_CHAT_ID, "text": text[:4096], "disable_web_page_preview": True}
    if reply_to:
        params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
    return await tg("sendMessage", **params)


async def tg_file(filename: str, content: bytes, mime: str, caption: str) -> dict:
    method, field = ("sendPhoto", "photo") if mime.startswith("image/") and mime != "image/webp" else ("sendDocument", "document")
    r = await http.post(
        f"{TG}/{method}",
        data={"chat_id": settings.TELEGRAM_CHAT_ID, "caption": caption[:1024]},
        files={field: (filename, content, mime)},
        timeout=120,
    )
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method}: {data.get('description')}")
    return data["result"]


async def tg_ack(message_id: int) -> None:
    try:
        await tg("setMessageReaction", chat_id=settings.TELEGRAM_CHAT_ID, message_id=message_id,
                 reaction=[{"type": "emoji", "emoji": "👍"}])
    except Exception:
        pass


# ---------------- WhatsApp (Meta Graph API) ----------------

def _auth() -> dict:
    return {"Authorization": f"Bearer {settings.META_ACCESS_TOKEN}"}


async def graph_post(path: str, body: dict) -> dict:
    r = await http.post(f"{GRAPH}/{path}", json=body, headers=_auth(), timeout=30)
    data = r.json()
    if r.status_code >= 400 or "error" in data:
        e = data.get("error", {})
        raise GraphError(f"{e.get('message', r.text)} (code {e.get('code')})")
    return data


async def wa_send_text(to: str, text: str) -> dict:
    return await graph_post(f"{settings.META_PHONE_NUMBER_ID}/messages", {
        "messaging_product": "whatsapp", "recipient_type": "individual", "to": to,
        "type": "text", "text": {"preview_url": False, "body": text[:4096]},
    })


async def wa_send_template(to: str, name: str, lang: str = "en_US") -> dict:
    return await graph_post(f"{settings.META_PHONE_NUMBER_ID}/messages", {
        "messaging_product": "whatsapp", "to": to, "type": "template",
        "template": {"name": name, "language": {"code": lang}},
    })


async def wa_mark_read(message_id: str) -> None:
    try:
        await graph_post(f"{settings.META_PHONE_NUMBER_ID}/messages",
                         {"messaging_product": "whatsapp", "status": "read", "message_id": message_id})
    except GraphError as e:
        log.warning("mark read failed: %s", e)


async def wa_download(media_id: str) -> tuple[bytes, str]:
    meta = (await http.get(f"{GRAPH}/{media_id}", headers=_auth(), timeout=30)).json()
    if "url" not in meta:
        raise GraphError(f"media {media_id}: {meta.get('error', {}).get('message', 'no url')}")
    r = await http.get(meta["url"], headers=_auth(), timeout=120)
    r.raise_for_status()
    return r.content, meta.get("mime_type", "application/octet-stream")


# ---------------- Webhook (Meta -> us) ----------------

def signature_ok(body: bytes, header: str) -> bool:
    expected = "sha256=" + hmac.new(settings.META_APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header or "", expected)


def _spawn(coro) -> None:
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global http
    store.init(settings.DB_PATH)
    http = httpx.AsyncClient()
    poller = asyncio.create_task(telegram_loop())
    yield
    poller.cancel()
    await http.aclose()


app = FastAPI(lifespan=lifespan)


@app.get("/webhook")
def verify(request: Request):
    q = request.query_params
    if q.get("hub.mode") == "subscribe" and hmac.compare_digest(q.get("hub.verify_token", ""), settings.META_VERIFY_TOKEN):
        return Response(q.get("hub.challenge", ""), media_type="text/plain")
    raise HTTPException(403, "verify token mismatch")


@app.post("/webhook")
async def receive(request: Request):
    body = await request.body()
    if not signature_ok(body, request.headers.get("X-Hub-Signature-256", "")):
        raise HTTPException(401, "bad signature")
    _spawn(handle_payload(json.loads(body)))      # answer Meta fast; work in the background
    return {"ok": True}


@app.get("/health")
def health():
    return {"ok": True}


async def handle_payload(payload: dict) -> None:
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            v = change.get("value", {})
            if v.get("metadata", {}).get("phone_number_id") != settings.META_PHONE_NUMBER_ID:
                continue
            names = {c.get("wa_id"): c.get("profile", {}).get("name", "") for c in v.get("contacts", [])}
            for m in v.get("messages", []):
                try:
                    await on_whatsapp(m, names.get(m.get("from"), ""))
                except Exception as e:
                    log.exception("forward failed")
                    await _safe_say(f"⚠️ Could not forward a WhatsApp message from +{m.get('from')}: {e}")
            for s in v.get("statuses", []):
                if s.get("status") == "failed":
                    err = (s.get("errors") or [{}])[0]
                    await _safe_say(f"⚠️ Not delivered to +{s.get('recipient_id')}: {err.get('title') or err.get('message')} "
                                    f"(code {err.get('code')})")


async def _safe_say(text: str) -> None:
    try:
        await tg_say(text)
    except Exception:
        log.exception("telegram send failed")


def _text_of(m: dict) -> str:
    t = m.get("type")
    if t == "text":
        return m["text"]["body"]
    if t == "button":
        return m["button"].get("text", "")
    if t == "interactive":
        i = m["interactive"]
        return (i.get("button_reply") or i.get("list_reply") or {}).get("title", "")
    if t == "location":
        loc = m["location"]
        return f"📍 {loc.get('name', '')} {loc.get('address', '')} https://maps.google.com/?q={loc['latitude']},{loc['longitude']}".strip()
    if t == "reaction":
        return f"reacted {m['reaction'].get('emoji', '')}"
    if t in ("image", "video", "audio", "document", "sticker"):
        return m[t].get("caption", "")
    return f"[{t} message, not supported]"


async def on_whatsapp(m: dict, name: str) -> None:
    if not store.first_time(m["id"]):
        return
    wa_id = m["from"]
    store.touch_contact(wa_id, name)
    header = f"💬 {name or 'Unknown'} (+{wa_id})"
    text = _text_of(m)
    t = m.get("type")
    if t in ("image", "video", "audio", "document", "sticker"):
        content, mime = await wa_download(m[t]["id"])
        filename = m[t].get("filename") or f"{t}.{mime.split('/')[-1].split(';')[0]}"
        sent = await tg_file(filename, content, mime, f"{header}\n{text}".strip())
    else:
        sent = await tg_say(f"{header}\n{text}")
    store.link(sent["message_id"], wa_id)
    await wa_mark_read(m["id"])


# ---------------- Telegram (you -> customer) ----------------

def _number(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    if not 8 <= len(digits) <= 15:
        raise ValueError(f"'{raw}' is not a phone number with country code, e.g. 919876543210")
    return digits


def in_window(wa_id: str) -> bool:
    last = store.last_inbound(wa_id)
    return last is not None and time.time() - last < WINDOW_S


async def send_free_text(wa_id: str, text: str, msg_id: int) -> None:
    if not in_window(wa_id):
        await tg_say(f"⏱ +{wa_id} has not written in the last 24 h, so WhatsApp only allows an approved template:\n"
                     f"/template {wa_id} <template_name> en_US", reply_to=msg_id)
        return
    await wa_send_text(wa_id, text)
    await tg_ack(msg_id)


async def on_telegram(msg: dict) -> None:
    text = (msg.get("text") or "").strip()
    mid = msg["message_id"]
    if not text:
        await tg_say("Only text replies are supported for now.", reply_to=mid)
        return
    if text.startswith(("/start", "/help")):
        await tg_say(HELP)
        return
    if text.startswith("/to "):
        m = re.match(r"^/to\s+(\+?\d[\d\s-]{6,}\d)\s+(.+)$", text, re.S)   # number may contain spaces or dashes
        if not m:
            await tg_say("Use: /to <number> <text>", reply_to=mid)
            return
        await send_free_text(_number(m.group(1)), m.group(2), mid)
        return
    if text.startswith("/template "):
        parts = text.split()
        if len(parts) < 3:
            await tg_say("Use: /template <number> <template_name> [lang]", reply_to=mid)
            return
        wa_id = _number(parts[1])
        await wa_send_template(wa_id, parts[2], parts[3] if len(parts) > 3 else "en_US")
        await tg_ack(mid)
        return
    reply_to = msg.get("reply_to_message")
    wa_id = store.wa_for(reply_to["message_id"]) if reply_to else None
    if not wa_id:
        await tg_say("Reply to a forwarded WhatsApp message (swipe left on it), or use /to. /help for more.", reply_to=mid)
        return
    await send_free_text(wa_id, text, mid)


async def telegram_loop() -> None:
    offset = int(store.get_kv("tg_offset", "0"))
    while True:
        try:
            updates = await tg("getUpdates", offset=offset, timeout=30, allowed_updates=["message"])
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("getUpdates: %s", e)          # 409 = another poller or a webhook is set on this bot
            await asyncio.sleep(5)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            store.set_kv("tg_offset", str(offset))
            msg = u.get("message")
            if not msg or str(msg["chat"]["id"]) != str(settings.TELEGRAM_CHAT_ID):
                continue                                # only the owner's chat may send to customers
            try:
                await on_telegram(msg)
            except (GraphError, ValueError, RuntimeError) as e:
                await _safe_say(f"⚠️ Not sent: {e}")
            except Exception:
                log.exception("telegram message failed")
                await _safe_say("⚠️ Not sent: unexpected error, see server log.")
