# db_connection.py
# MongoDB connection for Smart Exam Cell Queue System
# ─────────────────────────────────────────────

import os
import certifi
import pymongo
from pymongo.read_concern import ReadConcern
from pymongo.read_preferences import ReadPreference
from pymongo.write_concern import WriteConcern
from pathlib import Path
from dotenv import load_dotenv

# Ensure .env is loaded using absolute path
BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent
load_dotenv(BASE_DIR / '.env')

MONGO_URL = os.getenv("MONGO_URL")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "ExamCellQueueDB")

_client = None


def _uses_tls(url: str) -> bool:
    # Atlas (mongodb+srv://) always requires TLS; a plain local mongodb://localhost server
    # normally has it off, and forcing tls=True there fails the handshake outright. An
    # explicit tls=/ssl= option in the URL itself is left for pymongo to honour as written.
    lowered = url.lower()
    return lowered.startswith("mongodb+srv://") or "tls=true" in lowered or "ssl=true" in lowered


def get_db():
    global _client
    if _client is None:
        if not MONGO_URL:
            # Fail loud: pymongo.MongoClient(None) silently falls back to localhost, which
            # would connect to the wrong database undetected.
            print("[ExamCell] MONGO_URL environment variable is not set.")
            return None
        try:
            options = {"serverSelectionTimeoutMS": 5000}
            if _uses_tls(MONGO_URL):
                # tlsCAFile=certifi.where() pins TLS verification to certifi's maintained
                # Mozilla CA bundle instead of the OS certificate store — pymongo 3.11's
                # bundled OpenSSL can fail Atlas's handshake on Windows otherwise
                # (TLSV1_ALERT_INTERNAL_ERROR). Verification is not weakened in any way.
                options.update(tls=True, tlsCAFile=certifi.where())
            _client = pymongo.MongoClient(MONGO_URL, **options)
            # Ping database to force connection check
            _client.admin.command('ping')
            print(f"[ExamCell] MongoDB connected successfully - database: '{MONGO_DB_NAME}'")
        except Exception as e:
            print(f"[ExamCell] Failed to connect to MongoDB: {e}")
            _client = None
            return None
    return _client[MONGO_DB_NAME]


def get_client():
    """Returns the raw MongoClient (not a database) — needed for multi-document ACID
    transactions (client.start_session()), which operate at the client level."""
    if get_db() is None:  # ensures _client is connected, reuses the same connect-once logic
        return None
    return _client


_supports_transactions = None


def supports_transactions() -> bool:
    """Transactions need a replica set (every Atlas cluster is one) or a sharded cluster.
    A bare local `mongod` is standalone and would reject start_transaction()."""
    global _supports_transactions
    if _supports_transactions is None:
        client = get_client()
        if client is None:
            raise ConnectionError("[ExamCell] MongoDB not available")
        hello = client.admin.command("isMaster")
        _supports_transactions = bool(hello.get("setName")) or hello.get("msg") == "isdbgrid"
        if not _supports_transactions:
            print("[ExamCell] WARNING: MongoDB is standalone - multi-document transactions are "
                  "unavailable, falling back to single-document atomic updates only. Use Atlas "
                  "(or a local replica set) for full consistency guarantees.")
    return _supports_transactions


def run_transaction(callback):
    """
    Runs callback(session) inside a multi-document ACID transaction and returns its result.

    with_transaction() retries the whole callback on TransientTransactionError — which is
    exactly what a write conflict between two staff terminals racing on the same ticket or
    counter raises — so callbacks must only do database work (no external side effects)
    and must re-read everything they depend on. Any other exception aborts the transaction,
    rolling back every write the callback made, then propagates.
    """
    if not supports_transactions():
        return callback(None)
    with get_client().start_session() as session:
        return session.with_transaction(
            callback,
            read_concern=ReadConcern("snapshot"),
            write_concern=WriteConcern("majority"),
            read_preference=ReadPreference.PRIMARY,
        )


class _DbProxy:
    """
    Lazy MongoDB proxy.
    Handlers do: `from .db_connection import db_main`
    Each attribute access (e.g. db_main.tickets) calls get_db() so the
    connection is never attempted at import time.
    """
    def __getattr__(self, name: str):
        db = get_db()
        if db is None:
            raise ConnectionError("[ExamCell] MongoDB not available")
        return getattr(db, name)


# Singleton proxy — safe to import at module level
db_main = _DbProxy()
