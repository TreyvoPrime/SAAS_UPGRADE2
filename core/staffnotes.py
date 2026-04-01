from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.storage import read_json, write_json


def _utc_iso_now() -> str:
    return datetime.now(UTC).isoformat()


class StaffNoteStore:
    def __init__(self, path: str | Path = "dashboard_data/staff_notes.json"):
        self.path = Path(path)
        self.data = read_json(self.path, {"guilds": {}})

    def save(self) -> None:
        write_json(self.path, self.data)

    def _guild_bucket(self, guild_id: int) -> dict[str, Any]:
        guilds = self.data.setdefault("guilds", {})
        return guilds.setdefault(str(guild_id), {"counter": 0, "users": {}})

    def add_note(
        self,
        guild_id: int,
        user_id: int,
        *,
        moderator_id: int,
        moderator_name: str,
        note: str,
    ) -> dict[str, Any]:
        guild_bucket = self._guild_bucket(guild_id)
        guild_bucket["counter"] = int(guild_bucket.get("counter", 0)) + 1
        note_id = guild_bucket["counter"]
        users_bucket = guild_bucket.setdefault("users", {})
        notes = users_bucket.setdefault(str(user_id), [])
        payload = {
            "note_id": note_id,
            "moderator_id": int(moderator_id),
            "moderator_name": str(moderator_name),
            "note": str(note).strip(),
            "timestamp": _utc_iso_now(),
        }
        notes.append(payload)
        self.save()
        return payload

    def list_notes(self, guild_id: int, user_id: int) -> list[dict[str, Any]]:
        guild_bucket = self._guild_bucket(guild_id)
        users_bucket = guild_bucket.setdefault("users", {})
        notes = users_bucket.get(str(user_id), [])
        return list(notes)

    def remove_note(self, guild_id: int, user_id: int, note_id: int) -> bool:
        guild_bucket = self._guild_bucket(guild_id)
        users_bucket = guild_bucket.setdefault("users", {})
        user_key = str(user_id)
        notes = users_bucket.get(user_key, [])
        updated_notes = [note for note in notes if int(note.get("note_id", 0)) != int(note_id)]
        if len(updated_notes) == len(notes):
            return False
        if updated_notes:
            users_bucket[user_key] = updated_notes
        else:
            users_bucket.pop(user_key, None)
        self.save()
        return True
