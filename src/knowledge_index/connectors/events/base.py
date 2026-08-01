"""Extensible contract between a connector and the provider-event manager."""

from __future__ import annotations

import importlib
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.orm import sessionmaker

    from knowledge_index.config import AppConfig
    from knowledge_index.db.models import ConnectorEventSubscription, Source


@dataclass(frozen=True)
class DesiredSubscription:
    """One provider resource whose changes should wake a source."""

    target: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class UpstreamSubscription:
    """Provider state returned by create or renew."""

    external_id: str
    expires_at: datetime
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Coverage:
    """What event delivery can cover for one source right now."""

    mode: str  # live | reconciliation_only | waiting | unconfigured
    detail: str
    targets: int = 0


class ConnectorEventAdapter(ABC):
    """Provider-specific subscription lifecycle behind one connector-neutral seam."""

    key: str
    transport: str
    renew_before: timedelta

    def __init__(self, config: "AppConfig", session_factory: "sessionmaker") -> None:
        self.config = config
        self.session_factory = session_factory

    @property
    @abstractmethod
    def configured(self) -> bool:
        """Whether the appliance has the deployment-level event transport settings."""

    @abstractmethod
    def desired(self, source: "Source") -> tuple[list[DesiredSubscription], Coverage]:
        """Return provider resources implied by the source's current scope/cursor."""

    @abstractmethod
    def create(
        self, source: "Source", desired: DesiredSubscription
    ) -> UpstreamSubscription:
        """Create one upstream subscription."""

    @abstractmethod
    def renew(
        self, source: "Source", current: "ConnectorEventSubscription"
    ) -> UpstreamSubscription:
        """Renew one upstream subscription."""

    @abstractmethod
    def delete(self, source: "Source", current: "ConnectorEventSubscription") -> None:
        """Remove one upstream subscription."""

    def start_consumers(
        self,
        config_getter: Callable[[], "AppConfig"],
        handler: Callable[[str, list[Any]], bool],
    ) -> list[threading.Thread]:
        """Start this provider's outbound consumer, if configured.

        The default supports reconciliation-only adapters. A connector with live
        delivery owns its broker/parser here, which keeps the shared manager free of
        provider names and lets a plugin bring a new transport without a core branch.
        """
        return []


def load_adapter(reference: str, config: "AppConfig", session_factory: "sessionmaker"):
    """Load ``module:Class`` from a ConnectorSpec without adding core provider branches."""
    module_name, separator, class_name = reference.partition(":")
    if not separator or not module_name or not class_name:
        raise ValueError(f"invalid connector event adapter reference: {reference!r}")
    adapter_class = getattr(importlib.import_module(module_name), class_name)
    adapter = adapter_class(config, session_factory)
    if not isinstance(adapter, ConnectorEventAdapter):
        raise TypeError(f"{reference} is not a ConnectorEventAdapter")
    return adapter
