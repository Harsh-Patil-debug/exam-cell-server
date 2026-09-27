# status_check.py
# Simple server health check


def status_check():
    """Returns a simple status OK response."""
    return {
        "status": "ok",
        "message": "Smart Exam Cell Queue System API is running.",
        "version": "1.0.0"
    }
