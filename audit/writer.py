"""Audit write path. Two rules:

1. Every audited action goes through record(), never through a view or service
   hand-rolling an AuditLog row, so the metadata scrub cannot be bypassed.
2. Metadata keys that core.redaction treats as secrets are dropped before the
   row is written. An audit trail is a long-lived table full of exactly the
   payloads people would put a token into; the scrub belongs at the write, not
   at the read.

Writing an audit entry must never break the action it describes, so record()
swallows storage failures into a log line. A lost audit row is bad; a lost
task update with a traceback on top of it is worse.
"""

from __future__ import annotations

import logging

from audit.models import AuditLog
from core.redaction import SECRET_KEY_NAMES

logger = logging.getLogger(__name__)


def _scrub(metadata: dict | None) -> dict:
    """Drop any key the redaction layer would mask in a log line."""
    if not metadata:
        return {}
    return {k: v for k, v in metadata.items() if str(k).lower() not in SECRET_KEY_NAMES}


def record(actor, action: str, target_type: str, target_id=None, metadata: dict | None = None) -> AuditLog | None:
    """Append one audit entry. Returns the row, or None if the write failed."""
    try:
        return AuditLog.objects.create(
            actor=actor if getattr(actor, "pk", None) else None,
            action=action,
            target_type=target_type,
            target_id=str(target_id or ""),
            metadata=_scrub(metadata),
        )
    except Exception:  # noqa: BLE001 - the audit must not break the action
        logger.exception("audit write failed", extra={"audit_action": action, "audit_target": target_type})
        return None