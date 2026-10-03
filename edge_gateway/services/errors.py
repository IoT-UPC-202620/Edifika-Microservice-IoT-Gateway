"""Domain errors, mapped to HTTP responses by the API layer."""


class DomainError(Exception):
    status = 400
    status_name = "Bad Request"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class ValidationFailed(DomainError):
    status = 422
    status_name = "Unprocessable Entity"


class NotFoundError(DomainError):
    status = 404
    status_name = "Not Found"


class ConflictError(DomainError):
    status = 409
    status_name = "Conflict"


class DeviceUnavailable(ConflictError):
    """The target node is offline, in maintenance or unknown."""


class CommandNotConfirmed(DomainError):
    status = 504
    status_name = "Gateway Timeout"
