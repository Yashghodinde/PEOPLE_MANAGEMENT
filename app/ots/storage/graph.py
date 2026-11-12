"""Microsoft Graph storage provider (SharePoint document libraries).

Signs in as the app with a certificate (`Sites.Selected`, one site only). Files over 4 MiB use an upload
session in 5 MiB pieces whose progress is saved after every piece, so a crash resumes instead of restarting.
Every upload is checked after the last byte: size and QuickXorHash must match the local file.

Two rules from the plan are enforced here and tested:
* the pre-authenticated `uploadUrl` never receives an Authorization header;
* there is no generic delete (`delete_dr` works on the DR library only).
"""

from __future__ import annotations

import datetime
import email.utils
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from ots.core.clock import now

from .bandwidth import BandwidthLimiter
from .errors import ConfigurationError, GraphError, TransportFailure, VerificationFailed, WrongLibrary
from .provider import CHUNK, Library, OperationState, RemoteMeta, RemoteRef
from .quickxor import QuickXorHash

GRAPH = "https://graph.microsoft.com/v1.0"
SCOPE = "https://graph.microsoft.com/.default"
SMALL_LIMIT = 4 * 1024 * 1024  # up to 4 MiB: one PUT
PIECE = 5 * 1024 * 1024  # upload-session piece: a multiple of 320 KiB, as Graph requires
TOKEN_MARGIN = 300  # refresh the token 5 minutes before it expires
HASH_WAIT_SECONDS = 60
QUARANTINE = "_quarantine"
SELECT = "id,name,size,eTag,file,deleted,parentReference,lastModifiedDateTime"


class _SessionOp:
    """A throw-away operation for uploads that are not queue operations (the monthly manifest replace)."""

    def __init__(self, op_id: str) -> None:
        self.id = op_id
        self.upload_url = ""
        self.upload_expires_at: datetime.datetime | None = None
        self.confirmed_bytes = 0

    def save_session(self, upload_url: str, expires_at: datetime.datetime | None) -> None:
        self.upload_url, self.upload_expires_at = upload_url, expires_at

    def save_progress(self, confirmed_bytes: int) -> None:
        self.confirmed_bytes = confirmed_bytes

    def clear_session(self) -> None:
        self.upload_url, self.upload_expires_at, self.confirmed_bytes = "", None, 0


def _retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None
    if raw.strip().isdigit():
        return float(raw)
    try:  # an HTTP date
        when = email.utils.parsedate_to_datetime(raw)
        return max(0.0, (when - now()).total_seconds())
    except (TypeError, ValueError):
        return None


def _raise_for(resp: httpx.Response, *, url_kind: str = "") -> None:
    if resp.status_code < 400:
        return
    code, text = "", resp.text[:2048]
    try:
        err = resp.json().get("error", {})
        code = str(err.get("code", "")) if isinstance(err, dict) else ""
    except ValueError:
        pass
    raise GraphError(
        resp.status_code, code=code, body=text, retry_after=_retry_after(resp), url_kind=url_kind
    )


def _parse_time(text: str | None) -> datetime.datetime:
    if not text:
        return now()
    return datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))


class GraphProvider:
    def __init__(
        self,
        *,
        tenant_id: str,
        client_id: str,
        cert_path: Path,
        key_path: Path,
        site_id: str,
        drive_business: str,
        drive_dr: str,
        uplink_mbps: int = 0,
        version: str = "dev",
        client: httpx.Client | None = None,
        token_fn: Callable[[bool], tuple[str, int]] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        limiter: BandwidthLimiter | None = None,
    ) -> None:
        self.tenant_id, self.client_id = tenant_id, client_id
        self.cert_path, self.key_path = cert_path, key_path
        self.site_id = site_id
        self._drives = {Library.BUSINESS: drive_business, Library.DR: drive_dr}
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(connect=10.0, read=120.0, write=120.0, pool=10.0),
            http2=False,
            headers={"User-Agent": f"OTS/{version}"},
        )
        self._token_fn = token_fn or self._msal_token
        self._sleep = sleep
        self._mono = monotonic
        self._limiter = limiter or BandwidthLimiter(uplink_mbps, sleep=sleep)
        self._token = ""
        self._token_expires = 0.0
        self._msal_app: Any = None

    @classmethod
    def from_settings(cls) -> GraphProvider:
        from django.conf import settings

        missing = [
            n
            for n in (
                "GRAPH_TENANT_ID",
                "GRAPH_CLIENT_ID",
                "GRAPH_SITE_ID",
                "GRAPH_DRIVE_BUSINESS",
                "GRAPH_DRIVE_DR",
            )
            if not getattr(settings, n, "")
        ]
        if missing:
            raise ConfigurationError(f"Graph settings missing: {', '.join(missing)}")
        return cls(
            tenant_id=settings.GRAPH_TENANT_ID,
            client_id=settings.GRAPH_CLIENT_ID,
            cert_path=Path(settings.GRAPH_CERT_PATH),
            key_path=Path(settings.GRAPH_KEY_PATH),
            site_id=settings.GRAPH_SITE_ID,
            drive_business=settings.GRAPH_DRIVE_BUSINESS,
            drive_dr=settings.GRAPH_DRIVE_DR,
            uplink_mbps=settings.OTS_UPLINK_MBPS,
            version=settings.OTS_VERSION,
        )

    # ---- authentication ---------------------------------------------------------------------------
    def _load_certificate(self) -> Any:
        from cryptography import x509

        try:
            return x509.load_pem_x509_certificate(self.cert_path.read_bytes())
        except OSError as exc:
            raise ConfigurationError(f"cannot read the Graph certificate at {self.cert_path}: {exc}") from exc

    def certificate_not_after(self) -> datetime.datetime:
        """When the app certificate expires (checked daily; warning at 30 days, critical at 7)."""
        not_after: datetime.datetime = self._load_certificate().not_valid_after_utc
        return not_after

    def _msal_token(self, force: bool) -> tuple[str, int]:
        import msal
        from cryptography.hazmat.primitives import hashes

        if self._msal_app is None:
            cert = self._load_certificate()
            thumbprint = cert.fingerprint(hashes.SHA1()).hex().upper()  # noqa: S303 (Entra uses SHA-1 thumbprints)
            try:
                key = self.key_path.read_text()
            except OSError as exc:
                raise ConfigurationError(
                    f"cannot read the Graph private key at {self.key_path}: {exc}"
                ) from exc
            self._msal_app = msal.ConfidentialClientApplication(
                self.client_id,
                authority=f"https://login.microsoftonline.com/{self.tenant_id}",
                client_credential={"private_key": key, "thumbprint": thumbprint},
            )
        if force:  # msal keeps its own cache; drop it so a revoked token is not handed back
            self._msal_app.token_cache = msal.TokenCache()
        try:
            result = self._msal_app.acquire_token_for_client(scopes=[SCOPE])
        except Exception as exc:  # network trouble during sign-in
            raise TransportFailure(f"token request failed: {exc}") from exc
        if "access_token" not in result:
            raise GraphError(
                401,
                code=str(result.get("error", "token_failed")),
                body=str(result.get("error_description", "")),
            )
        return str(result["access_token"]), int(result.get("expires_in", 3600))

    def _bearer(self, *, force: bool = False) -> str:
        t = self._mono()
        if force or not self._token or t >= self._token_expires - TOKEN_MARGIN:
            token, lifetime = self._token_fn(force)
            self._token, self._token_expires = token, t + lifetime
        return self._token

    # ---- HTTP ---------------------------------------------------------------------------------------
    def _request(
        self, method: str, url: str, *, auth: bool = True, url_kind: str = "", **kw: Any
    ) -> httpx.Response:
        """One call. `auth=False` for pre-authenticated URLs: they never get an Authorization header."""
        base_headers = dict(kw.pop("headers", None) or {})
        for attempt in (1, 2):
            headers = dict(base_headers)
            if auth:
                headers["Authorization"] = f"Bearer {self._bearer(force=attempt == 2)}"
            try:
                resp = self._client.request(method, url, headers=headers, **kw)
            except httpx.TransportError as exc:
                raise TransportFailure(str(exc) or exc.__class__.__name__) from exc
            if resp.status_code == 401 and auth and attempt == 1:
                continue  # refresh the token once and try once more
            _raise_for(resp, url_kind=url_kind)
            return resp
        raise AssertionError("unreachable")  # pragma: no cover

    def _drive(self, library: Library) -> str:
        return self._drives[library]

    def _item_url(self, ref: RemoteRef) -> str:
        return f"{GRAPH}/drives/{ref.drive_id}/items/{ref.item_id}"

    def _path_url(self, library: Library, remote_path: str) -> str:
        return f"{GRAPH}/drives/{self._drive(library)}/root:/{quote(remote_path.strip('/'), safe='/')}"

    # ---- metadata -----------------------------------------------------------------------------------
    def _meta(self, library: Library, item: dict[str, Any]) -> RemoteMeta:
        drive_id = (item.get("parentReference") or {}).get("driveId") or self._drive(library)
        ref = RemoteRef(library, drive_id, str(item["id"]), item.get("eTag"))
        deleted = "deleted" in item
        parent = (item.get("parentReference") or {}).get("path", "")
        folder = parent.split("root:", 1)[1].strip("/") if "root:" in parent else ""
        name = str(item.get("name", ""))
        path = "/".join(p for p in (folder, name) if p) if name else ""
        hashes = (item.get("file") or {}).get("hashes") or {}
        return RemoteMeta(
            ref=ref,
            name=name,
            path=path,
            size=int(item.get("size", 0)),
            quick_xor_hash=hashes.get("quickXorHash"),
            deleted=deleted,
            last_modified=_parse_time(item.get("lastModifiedDateTime")),
        )

    def head(self, ref: RemoteRef) -> RemoteMeta:
        resp = self._request("GET", self._item_url(ref), params={"$select": SELECT})
        return self._meta(ref.library, resp.json())

    def find_by_path(self, library: Library, remote_path: str) -> RemoteMeta | None:
        try:
            resp = self._request("GET", self._path_url(library, remote_path), params={"$select": SELECT})
        except GraphError as exc:
            if exc.status == 404:
                return None
            raise
        return self._meta(library, resp.json())

    # ---- upload -------------------------------------------------------------------------------------
    def put(
        self, library: Library, remote_path: str, local_path: Path, size: int, sha256: str, op: OperationState
    ) -> RemoteRef:
        return self._upload(library, remote_path, local_path, size, sha256, op, conflict="fail")

    def replace_dr(self, remote_path: str, local_path: Path, size: int, sha256: str) -> RemoteRef:
        """The one allowed replace: a file in the DR library (the monthly manifest)."""
        return self._upload(
            Library.DR,
            remote_path,
            local_path,
            size,
            sha256,
            _SessionOp(f"replace:{remote_path}"),
            conflict="replace",
        )

    def _local_digests(self, local_path: Path, size: int, sha256: str) -> str:
        """Read the local file once: check it is still the file we recorded, return its QuickXorHash."""
        import hashlib

        sha, qx, total = hashlib.sha256(), QuickXorHash(), 0
        with local_path.open("rb") as fh:
            while chunk := fh.read(CHUNK):
                sha.update(chunk)
                qx.update(chunk)
                total += len(chunk)
        if total != size or sha.hexdigest() != sha256:
            raise VerificationFailed(f"local file changed since it was recorded: {local_path}")
        return qx.b64digest()

    def _upload(
        self,
        library: Library,
        remote_path: str,
        local_path: Path,
        size: int,
        sha256: str,
        op: OperationState,
        *,
        conflict: str,
    ) -> RemoteRef:
        local_qx = self._local_digests(local_path, size, sha256)
        if size <= SMALL_LIMIT:
            data = local_path.read_bytes()
            self._limiter.acquire(len(data))
            resp = self._request(
                "PUT",
                f"{self._path_url(library, remote_path)}:/content",
                params={"@microsoft.graph.conflictBehavior": conflict},
                content=data,
                headers={"Content-Type": "application/octet-stream"},
            )
            item = resp.json()
        else:
            item = self._upload_session(library, remote_path, local_path, size, op, conflict)
        meta = self._verify(library, item, size, local_qx)
        return RemoteRef(
            meta.ref.library, meta.ref.drive_id, meta.ref.item_id, meta.ref.etag, meta.quick_xor_hash
        )

    def _next_expected(self, url: str) -> int:
        resp = self._request("GET", url, auth=False, url_kind="upload")  # pre-authenticated: no Authorization
        ranges = resp.json().get("nextExpectedRanges") or []
        return int(str(ranges[0]).split("-", 1)[0]) if ranges else 0

    def _upload_session(
        self,
        library: Library,
        remote_path: str,
        local_path: Path,
        size: int,
        op: OperationState,
        conflict: str,
    ) -> dict[str, Any]:
        url = op.upload_url
        if url and op.upload_expires_at is not None and op.upload_expires_at <= now():
            op.clear_session()  # the session lapsed while we were away: start a new one
            url = ""
        if url:
            confirmed = self._next_expected(url)  # a 404 here is raised as GraphError(url_kind="upload")
        else:
            name = remote_path.strip("/").rsplit("/", 1)[-1]
            resp = self._request(
                "POST",
                f"{self._path_url(library, remote_path)}:/createUploadSession",
                json={"item": {"@microsoft.graph.conflictBehavior": conflict, "name": name}},
            )
            body = resp.json()
            url = str(body["uploadUrl"])
            op.save_session(url, _parse_time(body.get("expirationDateTime")))
            confirmed = 0
        op.save_progress(confirmed)
        stalled = 0
        with local_path.open("rb") as fh:
            while True:
                end = min(confirmed + PIECE, size)
                fh.seek(confirmed)
                piece = fh.read(end - confirmed)
                self._limiter.acquire(len(piece))
                try:
                    resp = self._request(
                        "PUT",
                        url,
                        auth=False,  # never send credentials to the pre-authenticated uploadUrl
                        url_kind="upload",
                        content=piece,
                        headers={"Content-Range": f"bytes {confirmed}-{end - 1}/{size}"},
                    )
                except GraphError as exc:
                    if exc.status != 416:
                        raise
                    stalled += 1
                    if stalled > 3:
                        raise
                    confirmed = self._next_expected(url)  # the server's idea of progress wins
                    op.save_progress(confirmed)
                    continue
                if resp.status_code in (200, 201):
                    return dict(resp.json())
                ranges = resp.json().get("nextExpectedRanges") or []
                confirmed = int(str(ranges[0]).split("-", 1)[0]) if ranges else end
                op.save_progress(confirmed)
                stalled = 0

    def _verify(self, library: Library, item: dict[str, Any], size: int, local_qx: str) -> RemoteMeta:
        meta = self._meta(library, item)
        if meta.size != size:
            raise VerificationFailed(f"remote size {meta.size} != local size {size} for {meta.path}")
        if size == 0:
            return meta
        deadline = self._mono() + HASH_WAIT_SECONDS
        while meta.quick_xor_hash is None:  # Graph sometimes fills the hash in a moment after the upload
            if self._mono() >= deadline:
                raise VerificationFailed(
                    f"no QuickXorHash was reported for {meta.path} within {HASH_WAIT_SECONDS}s"
                )
            self._sleep(5)
            meta = self.head(meta.ref)
            if meta.size != size:
                raise VerificationFailed(f"remote size {meta.size} != local size {size} for {meta.path}")
        if meta.quick_xor_hash != local_qx:
            raise VerificationFailed(f"remote hash does not match the local file for {meta.path}")
        return meta

    # ---- download -----------------------------------------------------------------------------------
    def open_read(self, ref: RemoteRef) -> Iterator[bytes]:
        """Stream the content in 1 MiB chunks. Graph answers 302 to a pre-authenticated URL; the client drops
        the Authorization header when it follows a redirect to another host."""
        try:
            with self._client.stream(
                "GET",
                f"{self._item_url(ref)}/content",
                headers={"Authorization": f"Bearer {self._bearer()}"},
                follow_redirects=True,
            ) as resp:
                if resp.status_code >= 400:
                    resp.read()
                    _raise_for(resp)
                yield from resp.iter_bytes(CHUNK)
        except httpx.TransportError as exc:
            raise TransportFailure(str(exc) or exc.__class__.__name__) from exc

    # ---- changes, usage, housekeeping -----------------------------------------------------------------
    def changes(self, library: Library, cursor: str | None) -> tuple[list[RemoteMeta], str]:
        url = cursor or f"{GRAPH}/drives/{self._drive(library)}/root/delta"
        if not url.startswith(f"{GRAPH}/"):
            raise ValueError("a delta cursor must be a Graph URL")  # never send the token elsewhere
        out: list[RemoteMeta] = []
        params: dict[str, str] | None = {"$select": SELECT} if cursor is None else None
        visited: set[str] = set()
        while True:
            if url in visited:  # a server that keeps sending the same page would loop us forever
                raise GraphError(502, code="badDelta", body=f"delta page repeated: {url[:200]}")
            visited.add(url)
            body = self._request("GET", url, params=params).json()
            params = None
            for item in body.get("value", []):
                if "deleted" in item or "file" in item:  # skip folders and the root
                    out.append(self._meta(library, item))
            if "@odata.nextLink" in body:
                url = str(body["@odata.nextLink"])
            elif "@odata.deltaLink" in body:
                return out, str(body["@odata.deltaLink"])
            else:
                raise GraphError(502, code="badDelta", body="delta answer has no nextLink or deltaLink")

    def usage(self) -> tuple[int, int]:
        body = self._request(
            "GET", f"{GRAPH}/drives/{self._drive(Library.BUSINESS)}", params={"$select": "quota"}
        ).json()
        quota = body.get("quota") or {}
        return int(quota.get("used", 0)), int(quota.get("total", 0))

    def delete_dr(self, ref: RemoteRef) -> None:
        if ref.library != Library.DR:
            raise WrongLibrary("business files are never deleted by code")
        self._request("DELETE", self._item_url(ref))

    def move_to_quarantine(self, ref: RemoteRef) -> RemoteRef:
        """Move an unknown item into /_quarantine/ (a PATCH of its parent, never a delete)."""
        folder = self._quarantine_folder(ref.library)
        meta = self.head(ref)
        resp = self._request(
            "PATCH",
            self._item_url(ref),
            json={
                "parentReference": {"id": folder},
                "name": f"{ref.item_id[:8]}-{meta.name}",
                "@microsoft.graph.conflictBehavior": "rename",
            },
        )
        item = resp.json()
        return RemoteRef(ref.library, ref.drive_id, str(item.get("id", ref.item_id)), item.get("eTag"))

    def _quarantine_folder(self, library: Library) -> str:
        drive = self._drive(library)
        try:
            return str(self._request("GET", f"{GRAPH}/drives/{drive}/root:/{QUARANTINE}").json()["id"])
        except GraphError as exc:
            if exc.status != 404:
                raise
        try:
            body = self._request(
                "POST",
                f"{GRAPH}/drives/{drive}/root/children",
                json={"name": QUARANTINE, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"},
            ).json()
        except GraphError as exc:
            if exc.status != 409:  # someone created it between our two calls
                raise
            return str(self._request("GET", f"{GRAPH}/drives/{drive}/root:/{QUARANTINE}").json()["id"])
        return str(body["id"])
