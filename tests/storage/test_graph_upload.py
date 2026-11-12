"""M05.2: GraphProvider uploads (small, large, resume, verification) against mocked Graph answers."""

import hashlib
import json

import httpx
import pytest
import respx
from ots.storage.errors import GraphError, TransportFailure, VerificationFailed
from ots.storage.graph import PIECE
from ots.storage.provider import Library
from ots.storage.quickxor import quickxor_b64

from tests.storage.conftest import MemoryOp
from tests.storage.graph_helpers import BIZ, UPLOAD_URL, G, item, make_provider, now_plus

CONTENT = f"{G}/drives/{BIZ}/root:/Files/2026/11/F-1:/content"
SESSION = f"{G}/drives/{BIZ}/root:/Files/2026/11/F-1:/createUploadSession"


def fileinfo(tmp_path, data, name="up.bin"):
    p = tmp_path / name
    p.write_bytes(data)
    return p, len(data), hashlib.sha256(data).hexdigest()


# --- small files -----------------------------------------------------------------------------------


@respx.mock
def test_small_upload_sends_one_put_with_conflict_fail_and_verifies(tmp_path):
    data = b"hello world" * 100
    path, size, sha = fileinfo(tmp_path, data)
    route = respx.put(CONTENT).mock(return_value=httpx.Response(201, json=item(data)))
    provider, _, _ = make_provider()
    ref = provider.put(Library.BUSINESS, "Files/2026/11/F-1", path, size, sha, MemoryOp())
    req = route.calls.last.request
    assert req.url.params["@microsoft.graph.conflictBehavior"] == "fail"
    assert req.headers["Authorization"] == "Bearer tok" and req.content == data
    assert (ref.library, ref.drive_id, ref.item_id) == (Library.BUSINESS, BIZ, "item-1")


@respx.mock
def test_hash_missing_at_first_is_fetched_again(tmp_path):
    data = b"abc" * 50
    path, size, sha = fileinfo(tmp_path, data)
    respx.put(CONTENT).mock(return_value=httpx.Response(201, json=item(data, qx=None)))
    head = respx.get(f"{G}/drives/{BIZ}/items/item-1").mock(
        side_effect=[httpx.Response(200, json=item(data, qx=None)), httpx.Response(200, json=item(data))]
    )
    provider, clock, _ = make_provider()
    provider.put(Library.BUSINESS, "Files/2026/11/F-1", path, size, sha, MemoryOp())
    assert head.call_count == 2 and clock.slept == [5, 5]


@respx.mock
def test_hash_that_never_arrives_is_a_verification_failure_after_60_seconds(tmp_path):
    data = b"abc" * 50
    path, size, sha = fileinfo(tmp_path, data)
    respx.put(CONTENT).mock(return_value=httpx.Response(201, json=item(data, qx=None)))
    respx.get(f"{G}/drives/{BIZ}/items/item-1").mock(
        return_value=httpx.Response(200, json=item(data, qx=None))
    )
    provider, clock, _ = make_provider()
    with pytest.raises(VerificationFailed, match="no QuickXorHash"):
        provider.put(Library.BUSINESS, "Files/2026/11/F-1", path, size, sha, MemoryOp())
    assert 55 <= sum(clock.slept) <= 70


@respx.mock
def test_size_or_hash_mismatch_is_a_verification_failure(tmp_path):
    data = b"x" * 1000
    path, size, sha = fileinfo(tmp_path, data)
    provider, _, _ = make_provider()
    respx.put(CONTENT).mock(return_value=httpx.Response(201, json=item(data, size=999)))
    with pytest.raises(VerificationFailed, match="size"):
        provider.put(Library.BUSINESS, "Files/2026/11/F-1", path, size, sha, MemoryOp())
    respx.put(CONTENT).mock(return_value=httpx.Response(201, json=item(data, qx=quickxor_b64(b"other"))))
    with pytest.raises(VerificationFailed, match="hash"):
        provider.put(Library.BUSINESS, "Files/2026/11/F-1", path, size, sha, MemoryOp())


@respx.mock
def test_a_local_file_that_changed_is_refused_before_any_request(tmp_path):
    path, size, sha = fileinfo(tmp_path, b"original")
    path.write_bytes(b"tampered")
    route = respx.put(CONTENT)
    provider, _, _ = make_provider()
    with pytest.raises(VerificationFailed, match="changed"):
        provider.put(Library.BUSINESS, "Files/2026/11/F-1", path, size, sha, MemoryOp())
    assert route.call_count == 0


@respx.mock
def test_conflict_is_a_409_with_graphs_code(tmp_path):
    data = b"z" * 10
    path, size, sha = fileinfo(tmp_path, data)
    respx.put(CONTENT).mock(
        return_value=httpx.Response(409, json={"error": {"code": "nameAlreadyExists", "message": "exists"}})
    )
    provider, _, _ = make_provider()
    with pytest.raises(GraphError) as exc:
        provider.put(Library.BUSINESS, "Files/2026/11/F-1", path, size, sha, MemoryOp())
    assert exc.value.status == 409 and exc.value.code == "nameAlreadyExists"


@respx.mock
def test_empty_file_needs_only_a_matching_size(tmp_path):
    path, size, sha = fileinfo(tmp_path, b"")
    respx.put(CONTENT).mock(return_value=httpx.Response(201, json=item(b"", qx=None)))
    provider, _, _ = make_provider()
    assert (
        provider.put(Library.BUSINESS, "Files/2026/11/F-1", path, size, sha, MemoryOp()).item_id == "item-1"
    )


# --- large files ---------------------------------------------------------------------------------


def big(n_pieces_plus=2 * 1024 * 1024):
    return (b"0123456789abcdef" * ((2 * PIECE + n_pieces_plus) // 16))[: 2 * PIECE + n_pieces_plus]


def mock_session(data):
    respx.post(SESSION).mock(
        return_value=httpx.Response(200, json={"uploadUrl": UPLOAD_URL, "expirationDateTime": now_plus(60)})
    )


@respx.mock
def test_large_upload_uses_a_session_in_5mib_pieces_and_never_sends_credentials_to_the_upload_url(tmp_path):
    data = big()
    path, size, sha = fileinfo(tmp_path, data)
    mock_session(data)
    puts = respx.put(UPLOAD_URL).mock(
        side_effect=[
            httpx.Response(202, json={"nextExpectedRanges": [f"{PIECE}-"]}),
            httpx.Response(202, json={"nextExpectedRanges": [f"{2 * PIECE}-"]}),
            httpx.Response(201, json=item(data)),
        ]
    )
    op = MemoryOp()
    provider, _, _ = make_provider()
    ref = provider.put(Library.BUSINESS, "Files/2026/11/F-1", path, size, sha, op)
    ranges = [c.request.headers["Content-Range"] for c in puts.calls]
    assert ranges == [
        f"bytes 0-{PIECE - 1}/{size}", f"bytes {PIECE}-{2 * PIECE - 1}/{size}", f"bytes {2 * PIECE}-{size - 1}/{size}"
    ]  # fmt: skip
    assert all("authorization" not in c.request.headers for c in puts.calls)
    assert b"".join(c.request.content for c in puts.calls) == data
    body = json.loads(respx.calls[0].request.content)
    assert body["item"]["@microsoft.graph.conflictBehavior"] == "fail" and body["item"]["name"] == "F-1"
    assert op.log[0] == ("session", UPLOAD_URL)
    assert [v for k, v in op.log if k == "progress"] == [0, PIECE, 2 * PIECE]
    assert ref.item_id == "item-1"


@respx.mock
def test_a_crash_at_piece_three_resumes_from_the_servers_progress(tmp_path):
    data = big()
    path, size, sha = fileinfo(tmp_path, data)
    session = respx.post(SESSION).mock(
        return_value=httpx.Response(200, json={"uploadUrl": UPLOAD_URL, "expirationDateTime": now_plus(60)})
    )
    op = MemoryOp()
    respx.put(UPLOAD_URL).mock(
        side_effect=[
            httpx.Response(202, json={"nextExpectedRanges": [f"{PIECE}-"]}),
            httpx.Response(202, json={"nextExpectedRanges": [f"{2 * PIECE}-"]}),
            httpx.ConnectError("network died"),
        ]
    )
    provider, _, _ = make_provider()
    with pytest.raises(TransportFailure):
        provider.put(Library.BUSINESS, "Files/2026/11/F-1", path, size, sha, op)
    assert op.upload_url == UPLOAD_URL and op.confirmed_bytes == 2 * PIECE  # survives for the next attempt
    sessions_created = session.call_count

    respx.reset()
    respx.get(UPLOAD_URL).mock(
        return_value=httpx.Response(200, json={"nextExpectedRanges": [f"{2 * PIECE}-{size - 1}"]})
    )
    last = respx.put(UPLOAD_URL).mock(return_value=httpx.Response(201, json=item(data)))
    again, _, _ = make_provider()  # a fresh provider, as after a worker restart
    ref = again.put(Library.BUSINESS, "Files/2026/11/F-1", path, size, sha, op)
    assert ref.item_id == "item-1"
    assert last.call_count == 1 and last.calls[0].request.content == data[2 * PIECE :]
    assert sessions_created == 1 and session.call_count == 0  # no second session was created
    assert "authorization" not in last.calls[0].request.headers


@respx.mock
def test_416_asks_the_server_where_to_continue(tmp_path):
    data = big()
    path, size, sha = fileinfo(tmp_path, data)
    mock_session(data)
    respx.get(UPLOAD_URL).mock(return_value=httpx.Response(200, json={"nextExpectedRanges": [f"{PIECE}-"]}))
    puts = respx.put(UPLOAD_URL).mock(
        side_effect=[
            httpx.Response(416, json={"error": {"code": "invalidRange"}}),
            httpx.Response(202, json={"nextExpectedRanges": [f"{2 * PIECE}-"]}),
            httpx.Response(201, json=item(data)),
        ]
    )
    provider, _, _ = make_provider()
    provider.put(Library.BUSINESS, "Files/2026/11/F-1", path, size, sha, MemoryOp())
    starts = [c.request.headers["Content-Range"].split(" ")[1].split("-")[0] for c in puts.calls]
    assert starts == ["0", str(PIECE), str(2 * PIECE)]


@respx.mock
def test_endless_416_gives_up(tmp_path):
    data = big()
    path, size, sha = fileinfo(tmp_path, data)
    mock_session(data)
    respx.get(UPLOAD_URL).mock(return_value=httpx.Response(200, json={"nextExpectedRanges": ["0-"]}))
    respx.put(UPLOAD_URL).mock(return_value=httpx.Response(416, json={"error": {"code": "invalidRange"}}))
    provider, _, _ = make_provider()
    with pytest.raises(GraphError) as exc:
        provider.put(Library.BUSINESS, "Files/2026/11/F-1", path, size, sha, MemoryOp())
    assert exc.value.status == 416


@respx.mock
def test_a_vanished_session_is_a_404_marked_as_the_upload_url(tmp_path):
    data = big()
    path, size, sha = fileinfo(tmp_path, data)
    op = MemoryOp(upload_url=UPLOAD_URL, upload_expires_at=None, confirmed_bytes=PIECE)
    respx.get(UPLOAD_URL).mock(return_value=httpx.Response(404, json={"error": {"code": "itemNotFound"}}))
    provider, _, _ = make_provider()
    with pytest.raises(GraphError) as exc:
        provider.put(Library.BUSINESS, "Files/2026/11/F-1", path, size, sha, op)
    assert exc.value.status == 404 and exc.value.url_kind == "upload"


@respx.mock
def test_a_session_past_its_expiry_is_replaced_not_resumed(tmp_path):
    import datetime

    data = big()
    path, size, sha = fileinfo(tmp_path, data)
    old = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=5)
    op = MemoryOp(upload_url="https://upload.example/old", upload_expires_at=old, confirmed_bytes=PIECE)
    session = respx.post(SESSION).mock(
        return_value=httpx.Response(200, json={"uploadUrl": UPLOAD_URL, "expirationDateTime": now_plus(60)})
    )
    respx.put(UPLOAD_URL).mock(
        side_effect=[
            httpx.Response(202, json={"nextExpectedRanges": [f"{PIECE}-"]}),
            httpx.Response(202, json={"nextExpectedRanges": [f"{2 * PIECE}-"]}),
            httpx.Response(201, json=item(data)),
        ]
    )
    provider, _, _ = make_provider()
    provider.put(Library.BUSINESS, "Files/2026/11/F-1", path, size, sha, op)
    assert session.call_count == 1 and ("cleared", None) in op.log


@respx.mock
def test_replace_dr_uses_conflict_replace_on_the_dr_library(tmp_path):
    data = b"manifest line\n"
    path, size, sha = fileinfo(tmp_path, data)
    url = f"{G}/drives/drive-dr/root:/Manifests/2026-11.jsonl:/content"
    route = respx.put(url).mock(
        return_value=httpx.Response(
            200,
            json=item(
                data, name="2026-11.jsonl", folder="/drives/drive-dr/root:/Manifests", drive="drive-dr"
            ),
        )
    )
    provider, _, _ = make_provider()
    ref = provider.replace_dr("Manifests/2026-11.jsonl", path, size, sha)
    assert route.calls.last.request.url.params["@microsoft.graph.conflictBehavior"] == "replace"
    assert ref.library == Library.DR and ref.drive_id == "drive-dr"
