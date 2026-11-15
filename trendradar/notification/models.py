# coding=utf-8
"""In-memory MIME payloads and SMTP submission results."""
from __future__ import annotations

from dataclasses import dataclass, field


def _addresses(value, name: str) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, (tuple, list)):
        raise ValueError(f"Invalid {name} sequence")
    result = tuple(value)
    if any(not isinstance(item, str) or not item or "\r" in item or "\n" in item for item in result):
        raise ValueError(f"Invalid {name} address")
    if len(set(result)) != len(result):
        raise ValueError(f"Duplicate {name} address")
    return result


@dataclass(frozen=True)
class PreparedEmail:
    """MIME bytes and envelope; no credentials."""

    sender: str = field(repr=False)
    recipients: tuple[str, ...] = field(repr=False)
    subject: str = field(repr=False)
    mime_bytes: bytes = field(repr=False)
    message_id: str = field(repr=False)
    date: str = field(repr=False)

    def __post_init__(self):
        object.__setattr__(self, "recipients", _addresses(self.recipients, "recipients"))
        if not self.recipients or not isinstance(self.mime_bytes, bytes) or not self.mime_bytes:
            raise ValueError("Invalid prepared email payload")
        for name in ("sender", "subject", "message_id", "date"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or "\r" in value or "\n" in value:
                raise ValueError(f"Invalid prepared email {name}")


@dataclass(frozen=True)
class EmailDeliveryResult:
    """One SMTP attempt; acceptance is not mailbox delivery."""

    configured: bool
    requested: tuple[str, ...] = field(default=(), repr=False)
    accepted: tuple[str, ...] = field(default=(), repr=False)
    temporary_failed: tuple[str, ...] = field(default=(), repr=False)
    permanent_failed: tuple[str, ...] = field(default=(), repr=False)
    unknown: tuple[str, ...] = field(default=(), repr=False)

    def __post_init__(self):
        if type(self.configured) is not bool:
            raise ValueError("Invalid configured flag")
        fields = ("requested", "accepted", "temporary_failed", "permanent_failed", "unknown")
        for name in fields:
            object.__setattr__(self, name, _addresses(getattr(self, name), name))
        outcomes = [address for name in fields[1:] for address in getattr(self, name)]
        if len(set(outcomes)) != len(outcomes) or set(outcomes) != set(self.requested):
            raise ValueError("Receipt outcomes must partition requested recipients")
        if self.accepted and not self.configured:
            raise ValueError("Unconfigured receipt cannot contain acceptance")

    @property
    def sent(self) -> bool:
        return self.configured and bool(self.requested) and set(self.accepted) == set(self.requested)

    @property
    def partially_delivered(self) -> bool:
        return bool(self.accepted) and not self.sent

    def __bool__(self) -> bool:
        raise TypeError("请显式检查 configured、sent、partially_delivered 或逐收件人结果")
