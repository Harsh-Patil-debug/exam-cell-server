# auth_middleware.py
# Resolves the logged-in account behind a request from its `Authorization: Bearer <access
# token>` header. Header-only on purpose: the frontend runs on a different origin, and
# bearer tokens can't be attached by a third-party page the way cookies can (no CSRF).

from rest_framework import status
from rest_framework.response import Response

from . import auth_handler


def _bearer_token(request):
    auth_header = request.headers.get("Authorization") or request.META.get("HTTP_AUTHORIZATION")
    if not auth_header or not auth_header.startswith("Bearer "):
        return None
    token = auth_header[len("Bearer "):].strip()
    return token or None


def authenticate_request(request, roles=None):
    """
    Returns (principal, None) on success, or (None, Response) to return as-is.
    principal = {"id", "_id", "role", "name", "email", ["roll"]}.
    roles: optional tuple of roles allowed on this endpoint (403 for any other role).
    """
    token = _bearer_token(request)
    if not token:
        return None, Response({"status": "error", "message": "Please log in to continue."},
                              status=status.HTTP_401_UNAUTHORIZED)
    try:
        principal = auth_handler.authenticate(token)
    except auth_handler.AuthError as e:
        return None, Response({"status": "error", "message": str(e), "code": "session_invalid"},
                              status=status.HTTP_401_UNAUTHORIZED)
    if roles and principal["role"] not in roles:
        return None, Response({"status": "error", "message": "You don't have access to this."},
                              status=status.HTTP_403_FORBIDDEN)
    return principal, None


def authenticate_staff_request(request):
    return authenticate_request(request, roles=("staff",))


def authenticate_student_request(request):
    return authenticate_request(request, roles=("student",))


def bearer_token(request):
    return _bearer_token(request)
