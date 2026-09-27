"""
Prints every collection in the configured MongoDB database with its document count —
a quick way to confirm the server is pointed at the database you think it is.

Usage (from server/):
    python check_collections.py
"""
from exam_project.main.Handlers.db_connection import get_db, MONGO_DB_NAME

db = get_db()
if db is None:
    raise SystemExit("[check] Could not connect — check MONGO_URL in server/.env")

names = sorted(db.list_collection_names())
print(f"[check] Database '{MONGO_DB_NAME}': {len(names)} collection(s)")
for name in names:
    print(f"  - {name}: {db[name].count_documents({})} document(s)")
