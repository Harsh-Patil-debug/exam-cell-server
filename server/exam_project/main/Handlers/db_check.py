# db_check.py
# Verifies MongoDB connectivity and read/write capability.

from datetime import datetime, timezone
from .db_connection import get_db


def db_check():
    """Insert a test document, read it back, delete it, and return the result."""

    db_main = get_db()
    if db_main is None:
        return {
            "status": "error",
            "message": "MongoDB connection is not established.",
            "db_connected": False,
        }

    test_collection = db_main["_connection_test"]

    try:
        # 1. Insert
        doc = {
            "ping": "examcell",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        insert_result = test_collection.insert_one(doc)
        inserted_id = str(insert_result.inserted_id)

        # 2. Read back
        fetched = test_collection.find_one({"_id": insert_result.inserted_id})

        # 3. Cleanup
        test_collection.delete_one({"_id": insert_result.inserted_id})

        if fetched is None:
            return {
                "status": "error",
                "db_connected": True,
                "write": "success",
                "read": "failed",
                "message": "Document was inserted but could not be read back.",
                "inserted_id": inserted_id,
            }

        return {
            "status": "ok",
            "db_connected": True,
            "write": "success",
            "read": "success",
            "cleanup": "success",
            "inserted_id": inserted_id,
            "document": {
                "ping": fetched.get("ping"),
                "timestamp": fetched.get("timestamp"),
            },
        }

    except Exception as e:
        return {
            "status": "error",
            "db_connected": True,
            "message": str(e),
        }
