from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote


_DATABASE_ENV_KEYS = ("DATABASE_URL", "POSTGRES_URL", "POSTGRESQL_URL")
_backend_lock = threading.Lock()
_storage_backend: PostgresDocumentBackend | None = None


def _load_psycopg():
    try:
        import psycopg  # type: ignore
    except ImportError as error:
        raise RuntimeError(
            "DATABASE_URL is set, but psycopg is not installed. Install requirements before starting ServerCore."
        ) from error
    return psycopg


def _postgres_dsn_from_env() -> str | None:
    for env_key in _DATABASE_ENV_KEYS:
        raw_value = os.getenv(env_key)
        if raw_value:
            return raw_value

    host = os.getenv("PGHOST")
    port = os.getenv("PGPORT")
    database = os.getenv("PGDATABASE")
    user = os.getenv("PGUSER")
    password = os.getenv("PGPASSWORD")
    if all([host, port, database, user, password]):
        return (
            f"postgresql://{quote(str(user))}:{quote(str(password))}"
            f"@{host}:{port}/{quote(str(database))}"
        )
    return None


def _normalize_document_key(path: Path) -> str:
    raw = path.as_posix()
    if path.is_absolute():
        try:
            raw = path.relative_to(Path.cwd()).as_posix()
        except ValueError:
            raw = path.resolve(strict=False).as_posix()
    return raw.lstrip("./") or path.name


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _storage_console_event(event: str, **fields: Any) -> None:
    payload = {
        "component": "storage",
        "event": event,
        "timestamp": _utcnow().isoformat(),
    }
    payload.update(fields)
    try:
        print("[storage]", json.dumps(payload, ensure_ascii=False, sort_keys=True))
    except Exception:
        print(f"[storage] {event} {fields}")


class PostgresDocumentBackend:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self._schema_ready = False
        self._schema_lock = threading.Lock()

    def _connect(self, *, autocommit: bool = True):
        psycopg = _load_psycopg()
        try:
            return psycopg.connect(self.dsn, autocommit=autocommit, connect_timeout=5)
        except Exception as error:
            _storage_console_event(
                "postgres_connect_failed",
                error=error.__class__.__name__,
                detail=str(error)[:240],
            )
            raise

    @contextmanager
    def _connection(self, *, autocommit: bool = True):
        with self._connect(autocommit=autocommit) as connection:
            yield connection

    def ensure_ready(self) -> None:
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS servercore_documents (
                            document_key TEXT PRIMARY KEY,
                            payload JSONB NOT NULL,
                            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        )
                        """
                    )
                    cursor.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_servercore_documents_updated_at
                        ON servercore_documents (updated_at DESC)
                        """
                    )
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS servercore_command_logs (
                            id BIGSERIAL PRIMARY KEY,
                            guild_id BIGINT NOT NULL,
                            timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                            kind TEXT,
                            status TEXT,
                            category TEXT,
                            actor_name TEXT,
                            command_name TEXT,
                            payload JSONB NOT NULL
                        )
                        """
                    )
                    cursor.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_servercore_command_logs_guild_timestamp
                        ON servercore_command_logs (guild_id, timestamp DESC)
                        """
                    )
                    cursor.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_servercore_command_logs_timestamp
                        ON servercore_command_logs (timestamp DESC)
                        """
                    )
            self._schema_ready = True

    def ping(self) -> bool:
        self.ensure_ready()
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT 1")
                    row = cursor.fetchone()
            return bool(row and int(row[0]) == 1)
        except Exception as error:
            _storage_console_event(
                "postgres_ping_failed",
                error=error.__class__.__name__,
                detail=str(error)[:240],
            )
            return False

    def read_document(self, key: str) -> tuple[bool, Any]:
        self.ensure_ready()
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT payload::text FROM servercore_documents WHERE document_key = %s",
                    (key,),
                )
                row = cursor.fetchone()
        if row is None:
            return False, None
        return True, json.loads(row[0])

    def write_document(self, key: str, payload: Any) -> None:
        self.write_documents({key: payload})

    def write_documents(self, documents: dict[str, Any]) -> None:
        if not documents:
            return
        self.ensure_ready()
        with self._connection(autocommit=False) as connection:
            with connection.cursor() as cursor:
                for key, payload in documents.items():
                    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
                    cursor.execute(
                        """
                        INSERT INTO servercore_documents (document_key, payload, updated_at)
                        VALUES (%s, %s::jsonb, NOW())
                        ON CONFLICT (document_key)
                        DO UPDATE SET payload = EXCLUDED.payload, updated_at = NOW()
                        WHERE servercore_documents.payload IS DISTINCT FROM EXCLUDED.payload
                        """
                        ,
                        (key, serialized),
                    )
            connection.commit()

    def append_command_log(self, entry: dict[str, Any]) -> None:
        self.ensure_ready()
        payload = dict(entry)
        timestamp = payload.pop("timestamp", None) or _utcnow().isoformat()
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO servercore_command_logs (
                        guild_id,
                        timestamp,
                        kind,
                        status,
                        category,
                        actor_name,
                        command_name,
                        payload
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    """,
                    (
                        int(entry.get("guild_id") or 0),
                        timestamp,
                        entry.get("kind"),
                        entry.get("status"),
                        entry.get("category"),
                        entry.get("user_name"),
                        entry.get("command"),
                        json.dumps({"timestamp": timestamp, **payload}, ensure_ascii=False, sort_keys=True),
                    ),
                )

    def list_command_logs(
        self,
        guild_id: int,
        limit: int = 100,
        *,
        query: str | None = None,
        kind: str | None = None,
        status: str | None = None,
        category: str | None = None,
        actor: str | None = None,
    ) -> list[dict[str, Any]]:
        self.ensure_ready()
        sql = [
            "SELECT payload::text FROM servercore_command_logs WHERE guild_id = %s",
        ]
        params: list[Any] = [int(guild_id)]
        if kind:
            sql.append("AND LOWER(COALESCE(kind, '')) = %s")
            params.append(str(kind).lower())
        if status:
            sql.append("AND LOWER(COALESCE(status, '')) = %s")
            params.append(str(status).lower())
        if category:
            sql.append("AND LOWER(COALESCE(category, '')) = %s")
            params.append(str(category).lower())
        if actor:
            sql.append("AND LOWER(COALESCE(actor_name, '')) LIKE %s")
            params.append(f"%{str(actor).strip().lower()}%")
        if query:
            needle = f"%{str(query).strip().lower()}%"
            sql.append(
                """
                AND (
                    LOWER(COALESCE(command_name, '')) LIKE %s
                    OR LOWER(COALESCE(actor_name, '')) LIKE %s
                    OR LOWER(payload::text) LIKE %s
                )
                """
            )
            params.extend([needle, needle, needle])
        sql.append("ORDER BY timestamp DESC LIMIT %s")
        params.append(max(1, int(limit)))

        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("\n".join(sql), tuple(params))
                rows = cursor.fetchall() or []
        return [json.loads(row[0]) for row in rows]

    def cleanup_command_logs(self, *, retention_days: int = 5) -> int:
        self.ensure_ready()
        cutoff = _utcnow() - timedelta(days=max(1, int(retention_days)))
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM servercore_command_logs WHERE timestamp < %s",
                    (cutoff.isoformat(),),
                )
                deleted = int(getattr(cursor, "rowcount", 0) or 0)
        return deleted


def _get_storage_backend() -> PostgresDocumentBackend | None:
    global _storage_backend
    dsn = _postgres_dsn_from_env()
    if not dsn:
        return None
    if _storage_backend is None:
        with _backend_lock:
            if _storage_backend is None:
                _storage_backend = PostgresDocumentBackend(dsn)
    return _storage_backend


def get_storage_backend() -> PostgresDocumentBackend | None:
    return _get_storage_backend()


def ensure_storage_ready() -> str:
    backend = _get_storage_backend()
    if backend is None:
        return "json"
    backend.ensure_ready()
    return "postgres"


def storage_backend_label() -> str:
    return "PostgreSQL" if _postgres_dsn_from_env() else "JSON files"


def _reset_storage_backend_cache() -> None:
    global _storage_backend
    with _backend_lock:
        _storage_backend = None


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _default_value(default: Any) -> Any:
    return copy.deepcopy(default)


def _backup_corrupt_file(path: Path) -> None:
    if not path.exists():
        return

    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    backup_path = path.with_name(f"{path.stem}.corrupt-{timestamp}{path.suffix}")
    try:
        path.replace(backup_path)
    except OSError:
        return


def _read_json_file(path: Path, default: Any) -> Any:
    if not path.exists():
        return _default_value(default)

    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
            return data
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        _backup_corrupt_file(path)
        return _default_value(default)


def read_json(path: Path, default: Any) -> Any:
    backend = _get_storage_backend()
    if backend is None:
        return _read_json_file(path, default)

    document_key = _normalize_document_key(path)
    found, payload = backend.read_document(document_key)
    if found:
        return payload

    if path.exists():
        file_payload = _read_json_file(path, default)
        if path.exists():
            backend.write_document(document_key, file_payload)
            _storage_console_event("document_migrated", document_key=document_key)
        return file_payload

    return _default_value(default)


def _write_json_file(path: Path, data: Any) -> None:
    ensure_parent(path)
    file_descriptor, temp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.stem}-",
        suffix=".tmp",
        text=True,
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def write_json(path: Path, data: Any) -> None:
    backend = _get_storage_backend()
    if backend is None:
        _write_json_file(path, data)
        return

    backend.write_document(_normalize_document_key(path), data)


def write_json_documents(documents: dict[Path | str, Any]) -> None:
    backend = _get_storage_backend()
    if backend is None:
        for raw_path, payload in documents.items():
            path = raw_path if isinstance(raw_path, Path) else Path(str(raw_path))
            _write_json_file(path, payload)
        return

    normalized = {
        _normalize_document_key(raw_path if isinstance(raw_path, Path) else Path(str(raw_path))): payload
        for raw_path, payload in documents.items()
    }
    backend.write_documents(normalized)


def run_storage_maintenance(*, retention_days: int = 5) -> dict[str, Any]:
    backend = _get_storage_backend()
    if backend is None:
        return {
            "backend": "json",
            "healthy": True,
            "deleted_logs": 0,
        }

    healthy = backend.ping()
    deleted_logs = 0
    if healthy:
        try:
            deleted_logs = backend.cleanup_command_logs(retention_days=retention_days)
        except Exception as error:
            healthy = False
            _storage_console_event(
                "command_log_cleanup_failed",
                error=error.__class__.__name__,
                detail=str(error)[:240],
            )
    result = {
        "backend": "postgres",
        "healthy": healthy,
        "deleted_logs": deleted_logs,
        "retention_days": retention_days,
    }
    _storage_console_event("maintenance", **result)
    return result
