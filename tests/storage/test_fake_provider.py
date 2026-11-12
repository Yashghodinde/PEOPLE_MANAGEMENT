"""M05.1: the fake provider honours the same rules as the real one (conflict=fail, no business delete ...)."""

import pytest
from ots.storage.errors import GraphError, VerificationFailed, WrongLibrary
from ots.storage.provider import Library
from ots.storage.quickxor import quickxor_b64

from tests.storage.conftest import MemoryOp


def test_put_then_read_back_matches(fake, local_file):
    path, size, sha = local_file(b"x" * 3_000_000)
    ref = fake.put(Library.BUSINESS, "Files/2026/11/F-1", path, size, sha, MemoryOp())
    assert b"".join(fake.open_read(ref)) == path.read_bytes()
    meta = fake.head(ref)
    assert meta.size == size and meta.path == "Files/2026/11/F-1" and meta.name == "F-1" and not meta.deleted
    assert meta.quick_xor_hash == quickxor_b64(path.read_bytes())
    assert fake.find_by_path(Library.BUSINESS, "Files/2026/11/F-1").ref.item_id == ref.item_id  # type: ignore[union-attr]


def test_open_read_streams_in_one_mebibyte_chunks(fake, local_file):
    path, size, sha = local_file(b"y" * (2 * 1024 * 1024 + 5))
    ref = fake.put(Library.BUSINESS, "a", path, size, sha, MemoryOp())
    assert [len(c) for c in fake.open_read(ref)] == [1024 * 1024, 1024 * 1024, 5]


def test_an_existing_name_is_a_409_never_overwritten(fake, local_file):
    p1, s1, h1 = local_file(b"first")
    p2, s2, h2 = local_file(b"second", "g.bin")
    fake.put(Library.BUSINESS, "same/name", p1, s1, h1, MemoryOp(id="a"))
    with pytest.raises(GraphError) as exc:
        fake.put(Library.BUSINESS, "same/name", p2, s2, h2, MemoryOp(id="b"))
    assert exc.value.status == 409 and exc.value.code == "nameAlreadyExists"
    assert b"".join(fake.open_read(fake.find_by_path(Library.BUSINESS, "same/name").ref)) == b"first"  # type: ignore[union-attr]


def test_put_is_idempotent_on_the_operation_id(fake, local_file):
    path, size, sha = local_file()
    a = fake.put(Library.BUSINESS, "p", path, size, sha, MemoryOp(id="same-op"))
    b = fake.put(Library.BUSINESS, "p", path, size, sha, MemoryOp(id="same-op"))
    assert a.item_id == b.item_id and len(fake.paths(Library.BUSINESS)) == 1


def test_a_local_file_that_changed_is_refused(fake, local_file):
    path, size, sha = local_file(b"abc")
    with pytest.raises(VerificationFailed):
        fake.put(Library.BUSINESS, "p", path, size + 1, sha, MemoryOp())
    with pytest.raises(VerificationFailed):
        fake.put(Library.BUSINESS, "p", path, size, "0" * 64, MemoryOp())


def test_libraries_are_separate(fake, local_file):
    path, size, sha = local_file()
    fake.put(Library.BUSINESS, "same", path, size, sha, MemoryOp(id="1"))
    fake.put(Library.DR, "same", path, size, sha, MemoryOp(id="2"))  # same name in the other library is fine
    assert fake.paths(Library.BUSINESS) == fake.paths(Library.DR) == {"same"}


def test_delete_dr_refuses_the_business_library_and_deletes_dr(fake, local_file):
    path, size, sha = local_file()
    biz = fake.put(Library.BUSINESS, "keep", path, size, sha, MemoryOp(id="1"))
    dr = fake.put(Library.DR, "prune-me", path, size, sha, MemoryOp(id="2"))
    with pytest.raises(WrongLibrary):
        fake.delete_dr(biz)
    assert fake.find_by_path(Library.BUSINESS, "keep") is not None  # still there
    fake.delete_dr(dr)
    assert fake.find_by_path(Library.DR, "prune-me") is None


def test_injected_failures_are_consumed_in_order(fake, local_file):
    fake.inject([503, None, 429], retry_after=120)
    with pytest.raises(GraphError) as e1:
        fake.usage()
    assert e1.value.status == 503
    assert fake.usage() == (0, fake.quota_total)  # None: passes
    with pytest.raises(GraphError) as e3:
        fake.usage()
    assert e3.value.status == 429 and e3.value.retry_after == 120
    assert fake.usage()[0] == 0  # queue empty: normal again


def test_quota_gives_507(fake, local_file):
    fake.set_quota(10)
    path, size, sha = local_file(b"x" * 11)
    with pytest.raises(GraphError) as exc:
        fake.put(Library.BUSINESS, "big", path, size, sha, MemoryOp())
    assert exc.value.status == 507
    ok, s2, h2 = local_file(b"x" * 10, "ok.bin")
    fake.put(Library.BUSINESS, "ok", ok, s2, h2, MemoryOp(id="2"))
    assert fake.usage() == (10, 10)


def test_missing_items_are_404(fake):
    from ots.storage.provider import RemoteRef

    ghost = RemoteRef(Library.BUSINESS, "d", "nope")
    with pytest.raises(GraphError) as exc:
        fake.head(ghost)
    assert exc.value.status == 404
    with pytest.raises(GraphError):
        list(fake.open_read(ghost))


def test_delta_log_reports_new_changed_renamed_and_deleted(fake, local_file):
    path, size, sha = local_file()
    a = fake.put(Library.BUSINESS, "a", path, size, sha, MemoryOp(id="1"))
    items, cursor = fake.changes(Library.BUSINESS, None)
    assert [m.path for m in items] == ["a"]
    assert fake.changes(Library.BUSINESS, cursor)[0] == []  # nothing new
    fake.remote_rename(a, "renamed")
    items, cursor = fake.changes(Library.BUSINESS, cursor)
    assert [m.path for m in items] == ["renamed"] and not items[0].deleted
    fake.remote_delete(a)
    items, cursor = fake.changes(Library.BUSINESS, cursor)
    assert len(items) == 1 and items[0].deleted and items[0].ref.item_id == a.item_id
    assert fake.changes(Library.DR, None)[0] == []  # other library untouched


def test_an_expired_cursor_is_a_410_once(fake):
    _, cursor = fake.changes(Library.BUSINESS, None)
    fake.expire_cursors()
    with pytest.raises(GraphError) as exc:
        fake.changes(Library.BUSINESS, cursor)
    assert exc.value.status == 410
    fake.changes(Library.BUSINESS, None)  # a fresh full listing works


def test_quarantine_moves_and_replace_dr_replaces_only_in_dr(fake, local_file):
    path, size, sha = local_file(b"v1")
    orphan = fake.plant(Library.BUSINESS, "Files/2026/11/stray", b"stray")
    moved = fake.move_to_quarantine(orphan)
    assert fake.find_by_path(Library.BUSINESS, "Files/2026/11/stray") is None
    assert any(p.startswith("_quarantine/") for p in fake.paths(Library.BUSINESS))
    assert moved.item_id == orphan.item_id  # moved, not copied or deleted
    first = fake.replace_dr("Manifests/2026-11.jsonl", path, size, sha)
    p2, s2, h2 = local_file(b"version two", "m2.bin")
    second = fake.replace_dr("Manifests/2026-11.jsonl", p2, s2, h2)
    assert b"".join(fake.open_read(second)) == b"version two" and first.item_id != second.item_id
