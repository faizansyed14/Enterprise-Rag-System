"""Periodic jobs run once per time slot: a finished job must not be queued again on the next tick of the same slot."""
import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from rag.jobs import scheduler


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def scalars(self):
        return list(self._rows)


def _fake_db(monkeypatch, already_in_table=()):
    queued: list[list[str]] = []
    asked: list[list[str]] = []

    class Conn:
        async def execute(self, statement, params=None):
            if "FROM jobs WHERE dedupe_key" in str(statement):
                asked.append(list(params["keys"]))
                return _Rows([k for k in params["keys"] if k in already_in_table])
            return _Rows([])  # no sources due, no users to sync

    @asynccontextmanager
    async def transaction():
        yield Conn()

    async def enqueue_many(conn, jobs):
        queued.append([j.kind for j in jobs])
        return len(jobs)

    monkeypatch.setattr(scheduler, "transaction", transaction)
    monkeypatch.setattr(scheduler.queue, "enqueue_many", enqueue_many)
    scheduler._HANDLED_SLOTS.clear()
    return queued, asked


SETTINGS = SimpleNamespace(auth_mode="local", entra_client_id="")
NOON = datetime(2026, 10, 23, 12, 0, 0, tzinfo=UTC)


def test_each_periodic_job_is_queued_once_per_slot_even_after_it_finished(monkeypatch):
    queued, asked = _fake_db(monkeypatch)
    asyncio.run(scheduler.tick(SETTINGS, NOON))
    assert set(queued[0]) == {kind for kind, *_ in scheduler.PERIODIC}
    asyncio.run(scheduler.tick(SETTINGS, NOON + timedelta(seconds=5)))  # same slots: the jobs may have finished meanwhile
    assert len(queued) == 1  # nothing queued again
    assert len(asked) == 1  # and the table was not asked again either
    asyncio.run(scheduler.tick(SETTINGS, NOON + timedelta(seconds=30)))  # a new 30-second slot
    assert sorted(queued[1]) == ["janitor", "monitor_tick"]  # reconcile stays once a day


def test_a_slot_another_scheduler_already_queued_is_skipped(monkeypatch):
    reconcile_slot = f"periodic:reconcile:{scheduler._slot(24 * 3600, NOON)}"
    queued, _ = _fake_db(monkeypatch, already_in_table={reconcile_slot})
    asyncio.run(scheduler.tick(SETTINGS, NOON))
    assert "reconcile" not in queued[0] and "janitor" in queued[0]
