# WhatsApp ↔ Telegram bridge (Meta Cloud API)

Customers write to your WhatsApp Business number. Each message shows up in your Telegram chat. You **reply** to it in
Telegram and the answer goes back to that customer on WhatsApp. Photos, voice notes and documents from customers are
forwarded as files.

```
Customer WhatsApp ──► Meta Cloud API ──POST /webhook──► this server ──► Telegram bot ──► your Telegram
Customer WhatsApp ◄── Meta Cloud API ◄──/messages───── this server ◄── getUpdates ◄──── your reply
```

## Files

| File | What it does |
|---|---|
| `setup_wizard.py` | Asks for the Telegram and Meta details, checks each one live, writes `.env` (chmod 600) |
| `app.py` | FastAPI server: Meta webhook in, Telegram poller, sends replies out |
| `store.py` | SQLite: Telegram message → customer number, 24 h window, de-duplication |
| `config.py` | Reads `.env` |
| `tests/test_bridge.py` | Offline tests (no network) |

## What you need before starting

**Telegram**
- A new bot from **@BotFather** (`/newbot`). Use a bot only for this; one bot can have only one listener.

**Meta (WhatsApp Cloud API)**
1. A Meta Business account at business.facebook.com.
2. An app at developers.facebook.com > Create app > type **Business** > add the **WhatsApp** product.
3. A phone number added under WhatsApp > API Setup. It must NOT be active in the WhatsApp phone app.
   The free test number works, but only to up to 5 numbers you verify there.
4. From **WhatsApp > API Setup**: the **Phone number ID** and the **WhatsApp Business Account ID**.
5. A permanent token: business.facebook.com > Settings > Users > **System users** > Add (Admin) >
   Assign assets (the app + the WhatsApp account, full control) > **Generate token** with
   `whatsapp_business_messaging` and `whatsapp_business_management`. The token on the API Setup page
   expires in 24 h, so do not use it beyond a first test.
6. From App settings > Basic: the **App ID** and **App secret**.

**Server**
- Python 3.10+ and a public **https** address for the webhook (domain + Caddy/nginx, or for testing
  `cloudflared tunnel --url http://localhost:8000`).

## Install and run

```bash
python -m venv venv && . venv/bin/activate
pip install -r requirements.txt
python setup_wizard.py                       # asks for everything, checks it, writes .env
uvicorn app:app --host 0.0.0.0 --port 8000   # ONE worker only
# in another terminal, with your public https URL:
python setup_wizard.py --webhook https://your.domain/webhook
```

The `--webhook` step registers the callback URL with Meta and subscribes the `messages` field. Meta checks it
right away, so the server must be running. You can also do it by hand: app > WhatsApp > Configuration >
Callback URL + Verify token (the wizard prints it), then Manage > subscribe **messages**.

## Using it (in Telegram)

- A customer message arrives as `💬 Name (+number)` and the text. **Reply** to it (swipe left) to answer.
  You get a 👍 on your message when it is sent, or a ⚠️ with Meta's reason when it is not.
- `/to <number> <text>` writes to a number without replying to a message.
- `/template <number> <template_name> [lang]` sends an approved template.
- `/help` lists the commands.

**24-hour rule (WhatsApp, not this code):** free text is allowed only within 24 h of the customer's last message.
After that, only an approved template can start the chat again (WhatsApp Manager > Message templates). The
bridge checks this and tells you to use `/template`.

## Safety

- Only the Telegram chat saved in `.env` can send to customers. Other chats are ignored.
- Every webhook call is checked with the `X-Hub-Signature-256` HMAC (App secret). Unsigned calls get 401.
- `.env` is chmod 600 and never logged. Do not commit it (`.gitignore` it).
- Meta retries webhooks, so message ids are de-duplicated.

## Run it as a service (Linux)

```ini
# /etc/systemd/system/wa-bridge.service
[Unit]
Description=WhatsApp Telegram bridge
After=network-online.target

[Service]
WorkingDirectory=/opt/wa-bridge
ExecStart=/opt/wa-bridge/venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

`systemctl enable --now wa-bridge`. Put Caddy in front: `your.domain { reverse_proxy 127.0.0.1:8000 }`.

## Common Meta errors

| Code | Meaning | Fix |
|---|---|---|
| 190 | Token expired or wrong | Use the System user token |
| 131030 | Number not in allowed list | Test number: add the recipient under API Setup |
| 131047 | Outside the 24 h window | Use `/template` |
| 132001 | Template name or language wrong | Check the exact name and language in WhatsApp Manager |
| 100 | Wrong Phone number ID | Use the ID, not the phone number |

## Tests

```bash
PYTHONPATH=. pytest -q tests
```
