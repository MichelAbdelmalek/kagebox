"""Rights records. Rendering is refused server-side unless the project's record is approved."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import model_validator

from pipeline.common.ids import ObjectRef, ProjectId, RightsId, UserId
from pipeline.common.schemas.base import ShortText, Strict

RightsBasis = Literal["owned", "licensed", "public_domain"]
RightsStatus = Literal["pending", "approved", "rejected"]


class RightsRecord(Strict):
    id: RightsId
    project_id: ProjectId
    basis: RightsBasis
    evidence_doc_ref: ObjectRef | None = None  # license, proof of ownership, or PD research
    justification: ShortText  # e.g. "Published 1941, copyright not renewed (DCM #1234)"
    licensor: ShortText | None = None
    territory: ShortText | None = None
    allows_cloud_processing: bool = False  # may pages be sent to a hosted model provider?
    expires_at: datetime | None = None
    status: RightsStatus = "pending"
    created_by: UserId
    approved_by: UserId | None = None
    approved_at: datetime | None = None

    @model_validator(mode="after")
    def _consistent(self) -> RightsRecord:
        if self.basis == "licensed" and not self.licensor:
            raise ValueError("a licensed work must name its licensor")
        if self.status == "approved":
            if self.evidence_doc_ref is None:
                raise ValueError("an approved record needs an evidence document")
            if self.approved_by is None or self.approved_at is None:
                raise ValueError("an approved record needs approved_by and approved_at")
            if self.approved_by == self.created_by:
                raise ValueError("a rights record cannot be approved by its creator")
        return self


class RightsDenied(PermissionError):
    """Raised when an operation is not permitted by the project's rights record."""


def require_render_allowed(record: RightsRecord | None, now: datetime) -> RightsRecord:
    """Gate used by the API, by every flow and again inside every render/audio task."""
    if record is None:
        raise RightsDenied("project has no rights record")
    if record.status != "approved":
        raise RightsDenied(f"rights record is {record.status}, not approved")
    if record.expires_at is not None and record.expires_at <= now:
        raise RightsDenied("rights record has expired")
    return record


def require_cloud_allowed(record: RightsRecord | None, now: datetime) -> RightsRecord:
    rec = require_render_allowed(record, now)
    if not rec.allows_cloud_processing:
        raise RightsDenied("rights record does not allow sending pages to a hosted provider")
    return rec
