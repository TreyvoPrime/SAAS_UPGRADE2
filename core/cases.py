from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.storage import read_json, write_json


CASE_DATA_PATH = Path("dashboard_data/moderation_cases.json")


class ModerationCaseStore:
    def __init__(self, path: Path | None = None):
        self.path = path or CASE_DATA_PATH
        self._data = read_json(self.path, {"guilds": {}})

    def _save(self) -> None:
        write_json(self.path, self._data)

    def _guild_key(self, guild_id: int) -> str:
        return str(guild_id)

    def _guild_bucket(self, guild_id: int) -> dict[str, Any]:
        guilds = self._data.setdefault("guilds", {})
        return guilds.setdefault(self._guild_key(guild_id), {"counter": 0, "cases": {}})

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _note_payload(*, entry_type: str, actor_id: int | None, actor_name: str, body: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = {
            "type": entry_type,
            "actor_id": actor_id,
            "actor_name": actor_name,
            "body": body,
            "timestamp": ModerationCaseStore._timestamp(),
        }
        if extra:
            payload.update(extra)
        return payload

    def create_case(
        self,
        guild_id: int,
        *,
        action: str,
        target_user_id: int,
        target_user_name: str,
        moderator_id: int,
        moderator_name: str,
        reason: str,
        duration_minutes: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        bucket = self._guild_bucket(guild_id)
        bucket["counter"] = int(bucket.get("counter", 0)) + 1
        case_id = bucket["counter"]
        case = {
            "case_id": case_id,
            "action": action,
            "target_user_id": int(target_user_id),
            "target_user_name": str(target_user_name),
            "moderator_id": int(moderator_id),
            "moderator_name": str(moderator_name),
            "reason": str(reason),
            "duration_minutes": int(duration_minutes) if duration_minutes is not None else None,
            "created_at": self._timestamp(),
            "metadata": metadata or {},
            "notes": [
                self._note_payload(
                    entry_type="action",
                    actor_id=moderator_id,
                    actor_name=moderator_name,
                    body=f"{action.title()} created.",
                    extra={"reason": str(reason)},
                )
            ],
        }
        bucket.setdefault("cases", {})[str(case_id)] = case
        self._save()
        return self.get_case(guild_id, case_id) or case

    def get_case(self, guild_id: int, case_id: int) -> dict[str, Any] | None:
        bucket = self._guild_bucket(guild_id)
        case = bucket.setdefault("cases", {}).get(str(case_id))
        return dict(case) if isinstance(case, dict) else None

    def add_note(
        self,
        guild_id: int,
        case_id: int,
        *,
        actor_id: int | None,
        actor_name: str,
        note: str,
    ) -> dict[str, Any] | None:
        bucket = self._guild_bucket(guild_id)
        case = bucket.setdefault("cases", {}).get(str(case_id))
        if not isinstance(case, dict):
            return None
        notes = case.setdefault("notes", [])
        notes.append(
            self._note_payload(
                entry_type="note",
                actor_id=actor_id,
                actor_name=actor_name,
                body=str(note).strip(),
            )
        )
        self._save()
        return dict(case)

    def list_cases(self, guild_id: int, limit: int = 25) -> list[dict[str, Any]]:
        bucket = self._guild_bucket(guild_id)
        cases = [
            dict(case)
            for case in bucket.setdefault("cases", {}).values()
            if isinstance(case, dict)
        ]
        cases.sort(key=lambda item: int(item.get("case_id", 0)), reverse=True)
        return cases[:limit]

    def list_user_cases(self, guild_id: int, user_id: int, limit: int = 25) -> list[dict[str, Any]]:
        bucket = self._guild_bucket(guild_id)
        cases = [
            dict(case)
            for case in bucket.setdefault("cases", {}).values()
            if isinstance(case, dict) and int(case.get("target_user_id", 0)) == int(user_id)
        ]
        cases.sort(key=lambda item: int(item.get("case_id", 0)), reverse=True)
        return cases[:limit]
