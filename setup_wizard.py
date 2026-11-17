"""Interactive setup: asks for the Telegram and Meta details, checks each one live, writes .env (chmod 600).

    python setup_wizard.py                         # ask everything
    python setup_wizard.py --webhook https://your.domain/webhook   # also register the Meta webhook (server must be running)
"""
import argparse
import getpass
import os
import secrets
import sys
import time
from pathlib import Path

import httpx
from dotenv import dotenv_values

ENV = Path(".env")
GRAPH_VERSION = "v23.0"


def ask(label: str, current: str | None = None, secret: bool = False, required: bool = True) -> str:
    hint = " [keep current]" if current else ""
    while True:
        val = (getpass.getpass if secret else input)(f"{label}{hint}: ").strip()
        if not val and current:
            return current
        if val or not required:
            return val
        print("  required")


def ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def fail(msg: str) -> None:
    print(f"  ✗ {msg}")


# ---------- Telegram ----------

def telegram(cur: dict) -> dict:
    print("\n== Telegram ==")
    print("1. In Telegram open @BotFather, send /newbot, pick a name. Copy the token it gives you.")
    print("   Use a NEW bot just for this bridge (a bot can have only one listener).")
    while True:
        token = ask("Telegram bot token", cur.get("TELEGRAM_BOT_TOKEN"), secret=True)
        r = httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=20).json()
        if r.get("ok"):
            bot = r["result"]["username"]
            ok(f"bot @{bot}")
            break
        fail(r.get("description", "token rejected"))

    chat_id = cur.get("TELEGRAM_CHAT_ID")
    if chat_id and input(f"Keep chat id {chat_id}? [Y/n]: ").strip().lower() != "n":
        return {"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": chat_id}

    print(f"2. Open https://t.me/{bot} and send /start (or add the bot to a group and send /start there).")
    print("   Waiting up to 2 minutes...")
    offset, deadline = 0, time.time() + 120
    while time.time() < deadline:
        r = httpx.get(f"https://api.telegram.org/bot{token}/getUpdates",
                      params={"offset": offset, "timeout": 20}, timeout=30).json()
        if not r.get("ok"):
            fail(r.get("description"))
            if r.get("error_code") == 409:
                print("  This bot has a webhook or another program reading it. Use a fresh bot.")
            sys.exit(1)
        for u in r["result"]:
            offset = u["update_id"] + 1
            chat = (u.get("message") or {}).get("chat")
            if chat:
                who = chat.get("title") or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
                if input(f"  Use chat '{who}' (id {chat['id']})? [Y/n]: ").strip().lower() != "n":
                    httpx.get(f"https://api.telegram.org/bot{token}/getUpdates", params={"offset": offset}, timeout=20)
                    ok("chat linked")
                    return {"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": str(chat["id"])}
    fail("no /start received")
    sys.exit(1)


# ---------- Meta / WhatsApp ----------

def meta(cur: dict) -> dict:
    print("\n== Meta WhatsApp Cloud API ==")
    print("Get these from https://developers.facebook.com/apps > your app > WhatsApp > API Setup,")
    print("and a permanent token from business.facebook.com > Settings > System users > Generate token")
    print("(permissions: whatsapp_business_messaging, whatsapp_business_management).")
    while True:
        token = ask("Access token", cur.get("META_ACCESS_TOKEN"), secret=True)
        phone_id = ask("Phone number ID (not the phone number)", cur.get("META_PHONE_NUMBER_ID"))
        r = httpx.get(f"https://graph.facebook.com/{GRAPH_VERSION}/{phone_id}",
                      params={"fields": "display_phone_number,verified_name,quality_rating"},
                      headers={"Authorization": f"Bearer {token}"}, timeout=20).json()
        if "error" not in r:
            ok(f"{r.get('verified_name')} {r.get('display_phone_number')} (quality {r.get('quality_rating', '?')})")
            break
        fail(r["error"].get("message"))

    waba = ask("WhatsApp Business Account ID", cur.get("META_WABA_ID"))
    r = httpx.post(f"https://graph.facebook.com/{GRAPH_VERSION}/{waba}/subscribed_apps",
                   headers={"Authorization": f"Bearer {token}"}, timeout=20).json()
    ok("app subscribed to this WhatsApp account") if r.get("success") else fail(f"subscribe: {r.get('error', {}).get('message')}")

    print("App secret: developers.facebook.com > your app > App settings > Basic > App secret (Show).")
    app_secret = ask("App secret", cur.get("META_APP_SECRET"), secret=True)
    app_id = ask("App ID (same page)", cur.get("META_APP_ID"))
    verify = cur.get("META_VERIFY_TOKEN") or secrets.token_urlsafe(24)
    return {"META_ACCESS_TOKEN": token, "META_PHONE_NUMBER_ID": phone_id, "META_WABA_ID": waba,
            "META_APP_SECRET": app_secret, "META_APP_ID": app_id, "META_VERIFY_TOKEN": verify,
            "GRAPH_VERSION": GRAPH_VERSION}


def register_webhook(env: dict, url: str) -> None:
    """Point the Meta app's WhatsApp webhook at our server. Meta calls GET <url> right away, so the server must run."""
    print(f"\n== Registering webhook {url} ==")
    r = httpx.post(f"https://graph.facebook.com/{GRAPH_VERSION}/{env['META_APP_ID']}/subscriptions",
                   params={"access_token": f"{env['META_APP_ID']}|{env['META_APP_SECRET']}"},
                   data={"object": "whatsapp_business_account", "callback_url": url,
                         "verify_token": env["META_VERIFY_TOKEN"], "fields": "messages"}, timeout=30).json()
    if r.get("success"):
        ok("webhook set, field 'messages' subscribed")
    else:
        fail(r.get("error", {}).get("message"))
        print("  Or set it by hand: app > WhatsApp > Configuration > Callback URL + Verify token, then subscribe 'messages'.")


def write_env(env: dict) -> None:
    lines = "".join(f"{k}={v}\n" for k, v in env.items())
    fd = os.open(ENV, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(lines)
    os.chmod(ENV, 0o600)
    ok(f"saved {ENV.resolve()} (only you can read it)")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--webhook", help="public https URL ending in /webhook; registers it with Meta")
    args = p.parse_args()
    cur = {k: v for k, v in dotenv_values(ENV).items() if v} if ENV.exists() else {}
    if args.webhook:
        if not cur:
            sys.exit("Run python setup_wizard.py first.")
        register_webhook(cur, args.webhook)
        return
    env = {**cur, **telegram(cur), **meta(cur)}
    env.setdefault("DB_PATH", "bridge.db")
    write_env(env)
    print(f"\nVerify token for the Meta dashboard: {env['META_VERIFY_TOKEN']}")
    print("Next: start the server, expose it on https, then: python setup_wizard.py --webhook https://<domain>/webhook")


if __name__ == "__main__":
    main()
