class RateLimitExceededError(Exception):
    """Custom exception for API rate limits."""
    pass


class UserNotFoundError(Exception):
    """Custom exception for when a user/profile cannot be found."""
    pass


class AccessForbiddenError(Exception):
    """Custom exception for access denied (e.g., private account)."""
    pass