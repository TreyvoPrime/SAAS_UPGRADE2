from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
from datetime import UTC, datetime
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


class PostgresDocumentBackend:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self._schema_ready = False
        self._schema_lock = threading.Lock()

    def _connect(self):
        psycopg = _load_psycopg()
        return psycopg.connect(self.dsn, autocommit=True, connect_timeout=5)

    def ensure_ready(self) -> None:
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            with self._connect() as connection:
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
            self._schema_ready = True

    def read_document(self, key: str) -> tuple[bool, Any]:
        self.ensure_ready()
        with self._connect() as connection:
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
        self.ensure_ready()
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO servercore_documents (document_key, payload, updated_at)
                    VALUES (%s, %s::jsonb, NOW())
                    ON CONFLICT (document_key)
                    DO UPDATE SET payload = EXCLUDED.payload, updated_at = NOW()
                    """,
                    (key, json.dumps(payload, ensure_ascii=False, sort_keys=True)),
                )


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
