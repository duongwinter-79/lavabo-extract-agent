"""Connector contract.

The whole point of the architecture: a connector's only job is to emit canonical
Conversations. Whether it polled an API or read a file a human dropped is invisible
to every stage downstream.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterator

from ..models import Conversation, Source


class Connector(ABC):
    source: Source

    @abstractmethod
    def fetch(self) -> Iterator[Conversation]:
        """Yield canonical conversations, chronologically sorted within each conversation."""

    def check(self) -> tuple[bool, str]:
        """Cheap preflight: credentials present, paths exist. (ok, human message)."""
        return True, "no preflight implemented"
