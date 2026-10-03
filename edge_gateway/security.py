"""Bearer-token protection for the local REST API."""

import hmac
from functools import wraps

from flask import current_app, request
from flask_smorest import abort


def require_token(view):
    """Reject the request with 401 unless it carries the gateway service token."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        header = request.headers.get("Authorization", "")
        scheme, _, token = header.partition(" ")
        expected = current_app.config["EDGE_CONFIG"].service_token
        if scheme.lower() != "bearer" or not hmac.compare_digest(token, expected):
            abort(401, message="Missing or invalid bearer token")
        return view(*args, **kwargs)

    return wrapper
