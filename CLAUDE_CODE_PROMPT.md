# Prompt for Claude Code

Clone this repo, open Claude Code in the folder and say: "follow CLAUDE_CODE_PROMPT.md".

---

Set up this WhatsApp ↔ Telegram bridge for me.

1. The code is already in this repo (read README.md first).
2. Make a venv, `pip install -r requirements.txt`, and run `PYTHONPATH=. pytest -q tests`. All 4 tests must pass.
3. Run `python setup_wizard.py` in the terminal and let me type my details. The wizard asks for my Telegram bot token,
   finds my chat id when I send /start, then asks for my Meta access token, Phone number ID, WhatsApp Business
   Account ID, App ID and App secret. It checks each one live and saves `.env` (chmod 600). Never print, log or
   commit the values in `.env`.
4. Start the server (`uvicorn app:app --port 8000`, one worker), give it a public https URL (my domain behind
   Caddy/nginx, or `cloudflared tunnel --url http://localhost:8000` for a test), then run
   `python setup_wizard.py --webhook https://<url>/webhook`.
5. Test: send a WhatsApp message to my business number, check it arrives in Telegram, reply to it, and check the
   reply reaches WhatsApp.
6. If I want it to keep running, set it up as the systemd service in README.md.

If Meta returns an error, look it up in the README's "Common Meta errors" table and tell me what to fix.
If a Graph API version is no longer supported, change GRAPH_VERSION in setup_wizard.py and .env to the current one.
