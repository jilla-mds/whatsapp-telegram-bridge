"""Small SQLite store: which Telegram message belongs to which WhatsApp customer, the customer's last message time
(for the 24-hour window), WhatsApp message ids already handled (Meta retries webhooks), and the Telegram offset."""
import sqlite3
import threading
import time

_lock = threading.Lock()
_db: sqlite3.Connection | None = None


def init(path: str) -> None:
    global _db
    _db = sqlite3.connect(path, check_same_thread=False)
    _db.executescript(
        """
        CREATE TABLE IF NOT EXISTS msg_map (tg_message_id INTEGER PRIMARY KEY, wa_id TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS contacts (wa_id TEXT PRIMARY KEY, name TEXT, last_inbound REAL);
        CREATE TABLE IF NOT EXISTS seen (wa_message_id TEXT PRIMARY KEY, at REAL);
        CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
        """
    )
    _db.commit()


def _run(sql: str, args: tuple = ()) -> list:
    with _lock:
        rows = _db.execute(sql, args).fetchall()
        _db.commit()
        return rows


def first_time(wa_message_id: str) -> bool:
    """True the first time a WhatsApp message id is seen, False on Meta's retries."""
    with _lock:
        cur = _db.execute("INSERT OR IGNORE INTO seen VALUES (?, ?)", (wa_message_id, time.time()))
        _db.commit()
        return cur.rowcount == 1


def touch_contact(wa_id: str, name: str) -> None:
    _run(
        "INSERT INTO contacts VALUES (?, ?, ?) ON CONFLICT(wa_id) DO UPDATE SET name=excluded.name, "
        "last_inbound=excluded.last_inbound",
        (wa_id, name, time.time()),
    )


def last_inbound(wa_id: str) -> float | None:
    rows = _run("SELECT last_inbound FROM contacts WHERE wa_id=?", (wa_id,))
    return rows[0][0] if rows else None


def contact_name(wa_id: str) -> str:
    rows = _run("SELECT name FROM contacts WHERE wa_id=?", (wa_id,))
    return rows[0][0] if rows and rows[0][0] else ""


def link(tg_message_id: int, wa_id: str) -> None:
    _run("INSERT OR REPLACE INTO msg_map VALUES (?, ?)", (tg_message_id, wa_id))


def wa_for(tg_message_id: int) -> str | None:
    rows = _run("SELECT wa_id FROM msg_map WHERE tg_message_id=?", (tg_message_id,))
    return rows[0][0] if rows else None


def get_kv(k: str, default: str | None = None) -> str | None:
    rows = _run("SELECT v FROM kv WHERE k=?", (k,))
    return rows[0][0] if rows else default


def set_kv(k: str, v: str) -> None:
    _run("INSERT OR REPLACE INTO kv VALUES (?, ?)", (k, v))
