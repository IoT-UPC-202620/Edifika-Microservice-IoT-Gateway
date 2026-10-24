"""Transport exceptions shared across cloud sync and AMQP publishers."""


class CloudUnavailable(Exception):
    """The backend cannot be reached or answered with a transient error: retry later."""


class CloudRejected(Exception):
    """The backend refused the request (4xx): retrying the same payload will not help."""
