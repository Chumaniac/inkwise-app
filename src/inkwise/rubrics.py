"""Immutable, local rule snapshots; exact applicable rules are never RRF-pruned."""

import hashlib
import json
import re
from pathlib import Path

from inkwise.contracts import JobCreate, Rubric
from inkwise.errors import DomainError


def safe_segment(value: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,119}", value) or ".." in value:
        raise DomainError("INVALID_IDENTIFIER", 422)
    return value


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def canonical(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class RubricRepository:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def active(self, tenant: str) -> dict:
        try:
            manifest = self.root / safe_segment(tenant) / "active.json"
            result = json.loads(manifest.read_text())
            if not isinstance(result, dict):
                raise ValueError("Invalid manifest")
            return result
        except (OSError, ValueError):
            raise DomainError("RULESET_UNAVAILABLE") from None

    def list(self, tenant: str) -> list[dict]:
        try:
            ids = self.active(tenant)
        except DomainError:
            return []
        return [self.get(tenant, rubric_id).model_dump(mode="json", by_alias=True) for rubric_id in ids]

    def get(self, tenant: str, rubric_id: str) -> Rubric:
        entry = self.active(tenant).get(rubric_id)
        if not isinstance(entry, dict) or "file" not in entry or "sha256" not in entry:
            raise DomainError("RULESET_UNAVAILABLE")
        try:
            path = self.root / safe_segment(tenant) / safe_segment(entry["file"])
            raw = path.read_text()
            if digest(raw) != entry["sha256"]:
                raise DomainError("RULESET_INTEGRITY_FAILED")
            rubric = Rubric.model_validate_json(raw)
            if rubric.id != rubric_id:
                raise DomainError("RULESET_INTEGRITY_FAILED")
            return rubric
        except (OSError, ValueError, TypeError):
            raise DomainError("RULESET_UNAVAILABLE") from None

    def for_job(self, tenant: str, payload: JobCreate) -> Rubric:
        rubric = self.get(tenant, payload.rubric_id)
        if (not rubric.grade_min <= payload.grade <= rubric.grade_max
                or rubric.language_policy != payload.language_policy or rubric.genre != payload.genre):
            raise DomainError("RULESET_SCOPE_MISMATCH")
        return rubric
