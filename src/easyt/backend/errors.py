"""What a backend raises when a model call gives no usable response."""


class BackendError(RuntimeError):
    """A model call on the backend failed."""


class UsageLimitReached(BackendError):
    """The subscription behind an agent CLI has reached its usage limit."""
