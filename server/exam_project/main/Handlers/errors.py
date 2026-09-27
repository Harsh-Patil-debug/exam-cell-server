# errors.py
# Raised inside a run_transaction() callback to abort it (rolling back every write made so
# far) and turn into an API error response. Handlers catch it and return
# ({"status": "error", "message": ...}, status_code).
#
# Also the project-wide DRF exception handler (settings.REST_FRAMEWORK["EXCEPTION_HANDLER"]),
# so an unexpected failure reaches the frontend as JSON it can show, never a Django HTML
# debug page.

from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError
from rest_framework.response import Response
from rest_framework.views import exception_handler


class QueueError(Exception):
    def __init__(self, message: str, status_code: int = 400, **extra):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.extra = extra

    def response(self):
        return {"status": "error", "message": self.message, **self.extra}, self.status_code


DB_UNAVAILABLE_MESSAGE = "The database is unreachable right now. Please try again in a minute."


def api_exception_handler(exc, context):
    # DRF's own exceptions (throttling, bad JSON, …) keep their normal responses.
    response = exception_handler(exc, context)
    if response is not None:
        return response
    if isinstance(exc, QueueError):
        body, status_code = exc.response()
        return Response(body, status=status_code)
    # db_connection raises ConnectionError when MongoDB can't be reached (Atlas IP allowlist,
    # network down, cluster paused); pymongo raises its own on a dropped connection.
    if isinstance(exc, (ConnectionError, ConnectionFailure, ServerSelectionTimeoutError)):
        print(f"[ExamCell] Database unavailable: {exc}")
        return Response({"status": "error", "message": DB_UNAVAILABLE_MESSAGE, "code": "db_unavailable"}, status=503)
    print(f"[ExamCell] Unhandled error in {context.get('view').__class__.__name__}: {exc!r}")
    return Response({"status": "error", "message": "Something went wrong on the server. Please try again."}, status=500)
