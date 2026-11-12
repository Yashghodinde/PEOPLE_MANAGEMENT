"""M05.2: GraphProvider authentication, errors, download, metadata, delta, quota, quarantine, bandwidth."""

import datetime

import httpx
import pytest
import respx
from freezegun import freeze_time
from ots.storage import graph as graph_module
from ots.storage.bandwidth import BandwidthLimiter
from ots.storage.errors import ConfigurationError, GraphError, TransportFailure, WrongLibrary
from ots.storage.graph import GraphProvider
from ots.storage.provider import CHUNK, Library, RemoteRef

from tests.storage.graph_helpers import BIZ, DR, G, item, make_provider

REF = RemoteRef(Library.BUSINESS, BIZ, "item-1")


# --- authentication -----------------------------------------------------------------------------


@respx.mock
def test_token_is_cached_until_five_minutes_before_expiry():
    route = respx.get(f"{G}/drives/{BIZ}/items/item-1").mock(
        return_value=httpx.Response(200, json=item(b"x"))
    )
    provider, clock, issued = make_provider()
    provider.head(REF)
    provider.head(REF)
    assert issued == [False]  # one sign-in served both calls
    clock.t += 3600 - 299  # inside the 5 minute margin
    provider.head(REF)
    assert issued == [False, False]
    assert route.call_count == 3


@respx.mock
def test_a_401_refreshes_the_token_once_and_retries_once():
    route = respx.get(f"{G}/drives/{BIZ}/items/item-1").mock(
        side_effect=[httpx.Response(401, json={"error": {"code": "InvalidAuthenticationToken"}}),
                     httpx.Response(200, json=item(b"x"))]
    )  # fmt: skip
    provider, _, issued = make_provider(tokens=["old", "new"])
    provider.head(REF)
    assert issued == [False, True]  # the retry forced a fresh token
    assert [c.request.headers["Authorization"] for c in route.calls] == ["Bearer old", "Bearer new"]


@respx.mock
def test_a_second_401_is_reported():
    respx.get(f"{G}/drives/{BIZ}/items/item-1").mock(return_value=httpx.Response(401, json={}))
    provider, _, _ = make_provider()
    with pytest.raises(GraphError) as exc:
        provider.head(REF)
    assert exc.value.status == 401


def test_msal_is_built_with_the_certificate_thumbprint(tmp_path, monkeypatch):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ots-test")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(1).not_valid_before(now).not_valid_after(now + datetime.timedelta(days=20))
        .sign(key, hashes.SHA256())
    )  # fmt: skip
    (tmp_path / "c.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "k.pem").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    seen = {}

    class FakeApp:
        def __init__(self, client_id, authority, client_credential):
            seen.update(client_id=client_id, authority=authority, cred=client_credential)

        def acquire_token_for_client(self, scopes):
            seen["scopes"] = scopes
            return {"access_token": "T", "expires_in": 3599}

    import msal

    monkeypatch.setattr(msal, "ConfidentialClientApplication", FakeApp)
    p = GraphProvider(
        tenant_id="tenant-1", client_id="client-1", cert_path=tmp_path / "c.pem", key_path=tmp_path / "k.pem",
        site_id="s", drive_business=BIZ, drive_dr=DR,
    )  # fmt: skip
    assert p._bearer() == "T"
    assert seen["authority"] == "https://login.microsoftonline.com/tenant-1" and seen["scopes"] == [
        graph_module.SCOPE
    ]
    assert seen["cred"]["thumbprint"] == cert.fingerprint(hashes.SHA1()).hex().upper()  # noqa: S303
    assert "BEGIN PRIVATE KEY" in seen["cred"]["private_key"]
    left = p.certificate_not_after() - now
    assert datetime.timedelta(days=19) < left < datetime.timedelta(days=21)


def test_a_missing_certificate_is_a_configuration_error(tmp_path):
    p = GraphProvider(
        tenant_id="t", client_id="c", cert_path=tmp_path / "none.pem", key_path=tmp_path / "none.key",
        site_id="s", drive_business=BIZ, drive_dr=DR,
    )  # fmt: skip
    with pytest.raises(ConfigurationError, match="certificate"):
        p.certificate_not_after()


def test_from_settings_names_what_is_missing(settings):
    settings.GRAPH_TENANT_ID = ""
    with pytest.raises(ConfigurationError, match="GRAPH_TENANT_ID"):
        GraphProvider.from_settings()


# --- error shapes ----------------------------------------------------------------------------------


@respx.mock
def test_429_carries_retry_after_seconds_and_503_a_date():
    respx.get(f"{G}/drives/{BIZ}/items/item-1").mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "120"}, json={"error": {"code": "activityLimitReached"}}),
                     httpx.Response(503, headers={"Retry-After": "not-a-number"}, json={})]
    )  # fmt: skip
    provider, _, _ = make_provider()
    with pytest.raises(GraphError) as e1:
        provider.head(REF)
    assert (e1.value.status, e1.value.retry_after, e1.value.code) == (429, 120.0, "activityLimitReached")
    with pytest.raises(GraphError) as e2:
        provider.head(REF)
    assert e2.value.status == 503 and e2.value.retry_after is None


@respx.mock
def test_the_error_body_is_kept_but_never_more_than_2kb():
    respx.get(f"{G}/drives/{BIZ}/items/item-1").mock(return_value=httpx.Response(400, text="x" * 10_000))
    provider, _, _ = make_provider()
    with pytest.raises(GraphError) as exc:
        provider.head(REF)
    assert len(exc.value.body) == 2048


@respx.mock
@pytest.mark.parametrize(
    "exc", [httpx.ConnectError("down"), httpx.ReadTimeout("slow"), httpx.ConnectTimeout("t")]
)
def test_network_trouble_is_a_transport_failure(exc):
    respx.get(f"{G}/drives/{BIZ}/items/item-1").mock(side_effect=exc)
    provider, _, _ = make_provider()
    with pytest.raises(TransportFailure):
        provider.head(REF)


# --- download ----------------------------------------------------------------------------------------


@respx.mock
def test_download_follows_the_redirect_without_credentials_and_streams_1mib_chunks():
    data = b"d" * (2 * CHUNK + 7)
    respx.get(f"{G}/drives/{BIZ}/items/item-1/content").mock(
        return_value=httpx.Response(302, headers={"Location": "https://cdn.sharepoint.example/dl/xyz"})
    )
    final = respx.get("https://cdn.sharepoint.example/dl/xyz").mock(
        return_value=httpx.Response(200, content=data)
    )
    provider, _, _ = make_provider()
    chunks = list(provider.open_read(REF))
    assert [len(c) for c in chunks] == [CHUNK, CHUNK, 7] and b"".join(chunks) == data
    assert "authorization" not in final.calls[0].request.headers  # pre-authenticated URL


@respx.mock
def test_download_of_a_missing_item_raises_404():
    respx.get(f"{G}/drives/{BIZ}/items/item-1/content").mock(
        return_value=httpx.Response(404, json={"error": {"code": "itemNotFound"}})
    )
    provider, _, _ = make_provider()
    with pytest.raises(GraphError) as exc:
        list(provider.open_read(REF))
    assert exc.value.status == 404


# --- metadata ----------------------------------------------------------------------------------------


@respx.mock
def test_head_builds_a_full_path_from_the_parent_folder():
    respx.get(f"{G}/drives/{BIZ}/items/item-1").mock(
        return_value=httpx.Response(200, json=item(b"abc", name="F-9"))
    )
    meta = make_provider()[0].head(REF)
    assert meta.path == "Files/2026/11/F-9" and meta.name == "F-9" and meta.size == 3 and not meta.deleted
    assert meta.ref.drive_id == BIZ and meta.quick_xor_hash


@respx.mock
def test_find_by_path_encodes_the_path_and_maps_404_to_none():
    route = respx.get(f"{G}/drives/{BIZ}/root:/Files/2026/11/a%20b%23c").mock(
        return_value=httpx.Response(404, json={"error": {"code": "itemNotFound"}})
    )
    provider, _, _ = make_provider()
    assert provider.find_by_path(Library.BUSINESS, "Files/2026/11/a b#c") is None
    assert route.call_count == 1
    respx.get(f"{G}/drives/{BIZ}/root:/Files/x").mock(
        return_value=httpx.Response(200, json=item(b"q", name="x"))
    )
    assert provider.find_by_path(Library.BUSINESS, "Files/x") is not None
    respx.get(f"{G}/drives/{BIZ}/root:/Files/boom").mock(return_value=httpx.Response(500, json={}))
    with pytest.raises(GraphError):
        provider.find_by_path(Library.BUSINESS, "Files/boom")  # only 404 means "absent"


# --- delta ---------------------------------------------------------------------------------------------


@respx.mock
def test_delta_pages_are_followed_to_the_delta_link_and_folders_are_skipped():
    base = f"{G}/drives/{BIZ}/root/delta"
    page2, final = f"{base}?token=p2", f"{base}?token=DONE"
    respx.get(base, params={"$select": graph_module.SELECT}).mock(
        return_value=httpx.Response(
            200,
            json={
                "value": [{"id": "root", "folder": {}}, item(b"a", item_id="i1", name="a")],
                "@odata.nextLink": page2,
            },
        )
    )
    respx.get(base, params={"token": "p2"}).mock(
        return_value=httpx.Response(
            200, json={"value": [{"id": "i2", "deleted": {"state": "deleted"}}], "@odata.deltaLink": final}
        )
    )
    items, cursor = make_provider()[0].changes(Library.BUSINESS, None)
    assert [(m.ref.item_id, m.deleted) for m in items] == [("i1", False), ("i2", True)]
    assert cursor == final
    respx.get(base, params={"token": "DONE"}).mock(
        return_value=httpx.Response(200, json={"value": [], "@odata.deltaLink": final + "2"})
    )
    assert make_provider()[0].changes(Library.BUSINESS, cursor) == ([], final + "2")


@respx.mock
def test_a_delta_that_repeats_a_page_is_stopped_not_looped_forever():
    loop = f"{G}/drives/{BIZ}/root/delta?token=again"
    respx.get(f"{G}/drives/{BIZ}/root/delta").mock(
        return_value=httpx.Response(200, json={"value": [], "@odata.nextLink": loop})
    )
    with pytest.raises(GraphError) as exc:
        make_provider()[0].changes(Library.BUSINESS, None)
    assert exc.value.status == 502 and exc.value.code == "badDelta"


@respx.mock
def test_an_expired_delta_cursor_is_a_410():
    url = f"{G}/drives/{BIZ}/root/delta?token=old"
    respx.get(url).mock(return_value=httpx.Response(410, json={"error": {"code": "resyncRequired"}}))
    with pytest.raises(GraphError) as exc:
        make_provider()[0].changes(Library.BUSINESS, url)
    assert exc.value.status == 410


def test_a_cursor_pointing_elsewhere_is_never_followed():
    with pytest.raises(ValueError, match="Graph URL"):
        make_provider()[0].changes(Library.BUSINESS, "https://evil.example/steal-the-token")


# --- usage, delete, quarantine ----------------------------------------------------------------------


@respx.mock
def test_usage_reads_the_drive_quota():
    respx.get(f"{G}/drives/{BIZ}").mock(
        return_value=httpx.Response(200, json={"quota": {"used": 70, "total": 100}})
    )
    assert make_provider()[0].usage() == (70, 100)


@respx.mock
def test_delete_dr_deletes_only_in_the_dr_library():
    route = respx.delete(f"{G}/drives/{DR}/items/old-wal").mock(return_value=httpx.Response(204))
    provider, _, _ = make_provider()
    provider.delete_dr(RemoteRef(Library.DR, DR, "old-wal"))
    assert route.call_count == 1
    with pytest.raises(WrongLibrary):
        provider.delete_dr(RemoteRef(Library.BUSINESS, BIZ, "precious"))
    assert route.call_count == 1  # nothing was sent for the business file


@respx.mock
def test_quarantine_creates_the_folder_then_moves_the_item():
    respx.get(f"{G}/drives/{BIZ}/root:/_quarantine").mock(
        return_value=httpx.Response(404, json={"error": {}})
    )
    made = respx.post(f"{G}/drives/{BIZ}/root/children").mock(
        return_value=httpx.Response(201, json={"id": "folder-q"})
    )
    respx.get(f"{G}/drives/{BIZ}/items/item-1").mock(
        return_value=httpx.Response(200, json=item(b"s", name="stray"))
    )
    patch = respx.patch(f"{G}/drives/{BIZ}/items/item-1").mock(
        return_value=httpx.Response(200, json={"id": "item-1"})
    )
    out = make_provider()[0].move_to_quarantine(REF)
    assert out.item_id == "item-1"
    assert made.calls[0].request.content and b'"_quarantine"' in made.calls[0].request.content
    sent = patch.calls[0].request.content
    assert b'"id":"folder-q"' in sent.replace(b" ", b"") and b"stray" in sent
    assert not [c for c in respx.calls if c.request.method == "DELETE"]  # moved, never deleted


# --- bandwidth ---------------------------------------------------------------------------------------


def limiter(mbps=8):
    waited = []
    t = [0.0]

    def sleep(s):
        waited.append(s)
        t[0] += s

    return BandwidthLimiter(mbps, clock=lambda: t[0], sleep=sleep), waited, t


def test_working_hours_are_limited_to_half_the_uplink_and_nights_are_free():
    lim, waited, _ = limiter(8)  # 8 Mbps * 0.5 = 4 Mbps = 500,000 bytes/s
    with freeze_time("2026-11-12 06:00:00"):  # 11:30 IST
        assert lim.rate_bytes_per_second() == 500_000
        lim.acquire(1_000_000)  # reserves 2 s of line
        lim.acquire(1_000_000)  # has to wait for them
    assert waited == [2.0]
    lim2, waited2, _ = limiter(8)
    with freeze_time("2026-11-12 18:00:00"):  # 23:30 IST
        assert lim2.rate_bytes_per_second() is None
        lim2.acquire(10**9)
    assert waited2 == []


@pytest.mark.parametrize(
    "utc,limited",
    [
        ("2026-11-12 03:29:59", False),
        ("2026-11-12 03:30:00", True),
        ("2026-11-12 13:29:59", True),
        ("2026-11-12 13:30:00", False),
    ],
)
def test_the_limit_applies_from_0900_to_1900_ist(utc, limited):
    lim, _, _ = limiter(8)
    with freeze_time(utc):
        assert (lim.rate_bytes_per_second() is not None) is limited


def test_zero_mbps_means_unlimited():
    lim, waited, _ = limiter(0)
    with freeze_time("2026-11-12 06:00:00"):
        lim.acquire(10**9)
    assert waited == []


@respx.mock
def test_uploads_pause_between_pieces_during_working_hours(tmp_path):
    import hashlib

    from ots.storage.graph import PIECE

    from tests.storage.conftest import MemoryOp
    from tests.storage.graph_helpers import UPLOAD_URL, now_plus

    data = b"q" * (PIECE + 100)
    path = tmp_path / "f"
    path.write_bytes(data)
    respx.post(f"{G}/drives/{BIZ}/root:/Files/2026/11/F-1:/createUploadSession").mock(
        return_value=httpx.Response(200, json={"uploadUrl": UPLOAD_URL, "expirationDateTime": now_plus(60)})
    )
    respx.put(UPLOAD_URL).mock(
        side_effect=[
            httpx.Response(202, json={"nextExpectedRanges": [f"{PIECE}-"]}),
            httpx.Response(201, json=item(data)),
        ]
    )
    provider, clock, _ = make_provider()
    provider._limiter = BandwidthLimiter(8, clock=clock, sleep=clock.sleep)
    with freeze_time("2026-11-12 06:00:00"):
        provider.put(
            Library.BUSINESS,
            "Files/2026/11/F-1",
            path,
            len(data),
            hashlib.sha256(data).hexdigest(),
            MemoryOp(),
        )
    assert clock.slept and clock.slept[0] == pytest.approx(PIECE / 500_000, rel=0.01)
