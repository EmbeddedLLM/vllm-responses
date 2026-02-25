from __future__ import annotations

from pathlib import Path

import pytest

from vtol.responses_core.store import DBResponseStore


@pytest.mark.anyio
async def test_sqlite_multi_worker_schema_init_requires_supervisor(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "state.db"
    store = DBResponseStore.from_db_url(db_url=f"sqlite+aiosqlite:///{db_path}")

    import vtol.responses_core.store as store_mod

    monkeypatch.setattr(store_mod.ENV_CONFIG, "workers", 2, raising=False)
    monkeypatch.delenv("VTOL_DB_SCHEMA_READY", raising=False)

    with pytest.raises(
        RuntimeError, match="SQLite schema initialization is not multi-worker safe"
    ):
        await store.ensure_schema()


@pytest.mark.anyio
async def test_schema_ready_env_skips_init(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "state.db"
    store = DBResponseStore.from_db_url(db_url=f"sqlite+aiosqlite:///{db_path}")

    monkeypatch.setenv("VTOL_DB_SCHEMA_READY", "1")

    # Should be a no-op and not create any files/tables.
    await store.ensure_schema()
    assert not db_path.exists()

    await store.aclose()


@pytest.mark.anyio
async def test_schema_init_uses_engine_begin_transaction(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "state.db"
    store = DBResponseStore.from_db_url(db_url=f"sqlite+aiosqlite:///{db_path}")

    begin_called = False

    from sqlalchemy.ext.asyncio import AsyncEngine

    original_begin = AsyncEngine.begin

    def _begin_wrapper(self, *args, **kwargs):
        nonlocal begin_called
        begin_called = True
        return original_begin(self, *args, **kwargs)

    monkeypatch.setattr(AsyncEngine, "begin", _begin_wrapper, raising=True)
    monkeypatch.delenv("VTOL_DB_SCHEMA_READY", raising=False)

    await store.ensure_schema()
    assert begin_called is True

    await store.aclose()
