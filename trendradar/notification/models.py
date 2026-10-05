# coding=utf-8
"""Immutable SMTP payloads and explicit, serializable submission receipts.

A receipt records SMTP acceptance, not mailbox delivery. Sensitive fields are
excluded from repr; callers persist these objects only in private storage.
"""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from typing import Any, Mapping


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
    """One frozen MIME message, with its original authorized envelope.

    Credentials and SMTP connection settings deliberately live outside this
    object. Retries change only the envelope subset, never these bytes/headers.
    """

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

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "sender": self.sender,
            "recipients": list(self.recipients),
            "subject": self.subject,
            "mime_base64": base64.b64encode(self.mime_bytes).decode("ascii"),
            "message_id": self.message_id,
            "date": self.date,
        }

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> PreparedEmail:
        try:
            if document["version"] != 1:
                raise ValueError("Unsupported prepared email version")
            return cls(
                sender=document["sender"], recipients=document["recipients"],
                subject=document["subject"],
                mime_bytes=base64.b64decode(document["mime_base64"], validate=True),
                message_id=document["message_id"], date=document["date"],
            )
        except (KeyError, TypeError, ValueError, binascii.Error) as exc:
            raise ValueError("Invalid prepared email document") from None


@dataclass(frozen=True)
class EmailDeliveryResult:
    """Disjoint, exhaustive outcomes for the requested envelope of one attempt.

    temporary_failed is safe to retry after the caller durably records this
    receipt. unknown is never an automatic retry candidate. RCPT acceptance
    alone is not acceptance here: DATA must receive its final 250 response.
    """

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

    def to_dict(self) -> dict[str, Any]:
        return {"version": 1, "configured": self.configured, **{
            name: list(getattr(self, name)) for name in (
                "requested", "accepted", "temporary_failed", "permanent_failed", "unknown"
            )
        }}

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> EmailDeliveryResult:
        try:
            if document["version"] != 1:
                raise ValueError("Unsupported receipt version")
            return cls(configured=document["configured"], **{
                name: document[name] for name in (
                    "requested", "accepted", "temporary_failed", "permanent_failed", "unknown"
                )
            })
        except (KeyError, TypeError, ValueError):
            raise ValueError("Invalid SMTP receipt document") from None
