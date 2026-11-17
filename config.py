"""Settings come from .env (written by setup_wizard.py). Nothing secret is ever logged."""
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

REQUIRED = [
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "META_ACCESS_TOKEN",
    "META_PHONE_NUMBER_ID",
    "META_APP_SECRET",
    "META_VERIFY_TOKEN",
]


@dataclass(frozen=True)
class Settings:
    TELEGRAM_BOT_TOKEN: str
    TELEGRAM_CHAT_ID: str
    META_ACCESS_TOKEN: str
    META_PHONE_NUMBER_ID: str
    META_APP_SECRET: str
    META_VERIFY_TOKEN: str
    META_WABA_ID: str = ""
    GRAPH_VERSION: str = "v23.0"
    DB_PATH: str = "bridge.db"


def load() -> Settings:
    missing = [k for k in REQUIRED if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"Missing in .env: {', '.join(missing)}. Run: python setup_wizard.py")
    fields = Settings.__dataclass_fields__
    return Settings(**{k: os.environ[k] for k in fields if os.environ.get(k)})


settings = load()
