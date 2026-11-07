from __future__ import annotations

import pytest

from engram.models import MemoryRecord, MemoryType
from engram.storage import SQLiteMemoryStore


@pytest.fixture()
def store(tmp_path) -> SQLiteMemoryStore:
    return SQLiteMemoryStore(tmp_path / "memory.db")


def test_insert_get_and_list(store: SQLiteMemoryStore) -> None:
    record = MemoryRecord(
        user_id="u1",
        content="I prefer Chinese explanations.",
        type=MemoryType.SEMANTIC,
        embedding=[0.1, 0.2, 0.3],
    )
    store.insert(record)

    loaded = store.get(record.id)
    assert loaded is not None
    assert loaded.content == record.content
    assert loaded.embedding == pytest.approx([0.1, 0.2, 0.3])

    records = store.list_memories("u1")
    assert [item.id for item in records] == [record.id]


def test_update_and_delete(store: SQLiteMemoryStore) -> None:
    record = MemoryRecord(user_id="u1", content="old")
    store.insert(record)
    record.content = "new"
    store.update(record)
    assert store.get(record.id).content == "new"  # type: ignore[union-attr]

    assert store.delete(record.id) is True
    assert store.get(record.id) is None
    assert store.delete(record.id) is False


def test_stats_and_export(store: SQLiteMemoryStore) -> None:
    store.insert(MemoryRecord(user_id="u1", content="a"))
    store.insert(MemoryRecord(user_id="u1", content="b", type=MemoryType.EPISODIC))

    stats = store.stats("u1")
    assert stats["total"] == 2
    assert stats["active"] == 2
    assert stats["by_type"]["semantic"] == 1
    assert stats["by_type"]["episodic"] == 1

    exported = store.export("u1")
    assert len(exported) == 2
    assert "embedding" in exported[0]


def test_upsert_rejects_cross_user_overwrite(store: SQLiteMemoryStore) -> None:
    source = MemoryRecord(user_id="u1", content="owned by u1")
    store.upsert(source)
    hijack = MemoryRecord(id=source.id, user_id="attacker", content="replaced")
    with pytest.raises(ValueError, match="another tenant/user"):
        store.upsert(hijack)
    stored = store.get(source.id)
    assert stored is not None
    assert stored.user_id == "u1"
    assert stored.content == "owned by u1"
