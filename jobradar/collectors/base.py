"""Common interface for all collectors."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Vacancy


class CollectorError(RuntimeError):
    """Raised when a source cannot be read at all (blocked, layout changed, auth failed)."""


class Collector(ABC):
    name: str = "base"

    @abstractmethod
    def collect(self) -> list[Vacancy]:
        """Return vacancy stubs from all configured searches (may be partially filled)."""

    def enrich(self, v: Vacancy) -> Vacancy:
        """Fill in full description / company / salary. Default: nothing to add."""
        return v
