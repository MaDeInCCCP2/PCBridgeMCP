"""Exclusive, expiring mutation grants for one owned resource."""
from time import monotonic

from computer_use import ComputerUseError


class MutationLease:
    """All methods must be called under the resource's dispatch lock."""
    def __init__(self, clock=monotonic):
        self.clock = clock
        self.generation = 0
        self.deadline = 0

    def _require(self, fence, now):
        if not self.generation or fence != self.generation or now >= self.deadline:
            raise ComputerUseError("lease_stale", "The mutation lease was replaced, released or expired.")

    def acquire(self, seconds, renew_fence=None):
        if type(seconds) is not int or not 1 <= seconds <= 120:
            raise ComputerUseError("lease_duration", "Lease duration must be between 1 and 120 seconds.")
        now = self.clock()
        if renew_fence is not None:
            self._require(renew_fence, now)
        else:
            if now < self.deadline:
                raise ComputerUseError("lease_held", "A mutation lease is already active for this resource.")
            self.generation += 1
        self.deadline = now + seconds
        return {"fence": self.generation, "expires_in_seconds": seconds}

    def release(self, fence):
        self._require(fence, self.clock())
        self.deadline = 0

    def authorize(self, fence):
        if not self.generation and fence is None:
            return  # Sessions created for existing callers begin in compatibility mode.
        self._require(fence, self.clock())
