"""Read-modify-write of ``sync-data.json`` with optimistic locking.

Write cycle (mirrors ``FileBasedSyncAdapterService._uploadOps``):

1. Download the current file (strong etag from Nextcloud).
2. Run the mutation against a deep copy of the snapshot; it emits operations.
3. Build the new envelope: ``syncVersion + 1``, ops tagged with that ``sv``,
   vector clock incremented for this client per op, ``recentOps`` trimmed.
4. Write the previous content to ``sync-data.json.<UTC stamp>.sv<N>.bak``
   (recovery artifact, ``N`` = the syncVersion being replaced); backups older
   than ``SP_BACKUP_RETENTION_DAYS`` are pruned after a successful upload, at
   most once an hour.
5. Conditional PUT (``If-Match``); on 412 start over with the fresh remote.
6. Take the new revision from the PUT response's ``OC-ETag``; optionally
   (``SP_VERIFY_UPLOAD``) re-download and compare hashes instead.

Decoding, encoding and the snapshot copy are CPU-bound on multi-MB files and
run in a worker thread so other tool calls keep being served.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar
from zoneinfo import ZoneInfo

from . import codec
from .config import Settings
from .ids import generate_client_id, is_usable_client_id
from .ops import PendingOp, build_compact_op
from .syncfile import (
    FILE_VERSION,
    MAX_RECENT_OPS,
    SYNC_FILE,
    SyncFile,
    backup_file_name,
    backup_timestamp,
    increment_clock,
    merge_clocks,
    validate_envelope,
)
from .timeutil import get_start_of_next_day_diff_ms, today_str
from .webdav import Locked, NextcloudDav, NotFound, PreconditionFailed

log = logging.getLogger(__name__)
T = TypeVar("T")
PRUNE_INTERVAL_S = 3600.0


class SyncError(Exception):
    pass


class ConflictError(SyncError):
    pass


@dataclass
class ClientIdentity:
    client_id: str
    counter: int = 0
    path: Path | None = None

    @classmethod
    def load(cls, data_dir: Path, configured_id: str | None) -> ClientIdentity:
        path = data_dir / "client.json"
        stored: dict[str, Any] = {}
        try:
            if path.exists():
                stored = json.loads(path.read_text("utf-8"))
        except Exception as e:  # noqa: BLE001
            log.warning("Could not read %s: %s", path, e)
        client_id = configured_id or stored.get("clientId")
        if not is_usable_client_id(client_id):
            if configured_id:
                raise ValueError("SP_CLIENT_ID must be at least 5 characters of [A-Za-z0-9_-]")
            client_id = generate_client_id()
            log.info("Generated new sync client id %s", client_id)
        counter = 0
        if stored.get("clientId") == client_id:
            try:
                counter = max(0, int(stored.get("counter") or 0))
            except TypeError, ValueError:
                log.warning("Ignoring unreadable counter %r in %s", stored.get("counter"), path)
        ident = cls(client_id=client_id, counter=counter, path=path)
        ident.save()
        return ident

    def save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"clientId": self.client_id, "counter": self.counter}), "utf-8")
            tmp.replace(self.path)
        except Exception as e:  # noqa: BLE001
            log.warning("Could not persist client identity to %s: %s", self.path, e)


class MutationContext:
    """What a mutation sees: a private copy of the snapshot plus emit().

    With ``copy_state=False`` the context shares the cached snapshot instead;
    that variant is for read-only tools and must never be handed to a mutation.
    """

    def __init__(self, sync_file: SyncFile, tz: ZoneInfo, now_ms: int, *, copy_state: bool = True) -> None:
        self.sync_file = sync_file
        self.state: dict[str, Any] = copy.deepcopy(sync_file.state) if copy_state else sync_file.state
        self.tz = tz
        self.now_ms = now_ms
        self.start_of_next_day_diff_ms = get_start_of_next_day_diff_ms(self.state.get("globalConfig"))
        self.today = today_str(now_ms, tz, self.start_of_next_day_diff_ms)
        self.ops: list[PendingOp] = []

    def emit(self, action: str, entity_id: str, action_payload: dict[str, Any]) -> None:
        self.ops.append(PendingOp(action, entity_id, action_payload))


def _strip_local_only_sync_settings(state: dict[str, Any]) -> None:
    gc = state.get("globalConfig")
    if not isinstance(gc, dict):
        return
    sync = gc.get("sync")
    if not isinstance(sync, dict):
        return
    sync.pop("syncInterval", None)
    sync.pop("isManualSyncOnly", None)
    sync["syncProvider"] = None


class SyncStore:
    def __init__(
        self,
        dav: NextcloudDav,
        identity: ClientIdentity,
        *,
        password: str | None,
        allow_plaintext: bool,
        tz: ZoneInfo,
        cache_ttl: float = 10.0,
        verify_upload: bool = False,
        write_backup: bool = True,
        backup_retention_days: int = 7,
        max_attempts: int = 3,
    ) -> None:
        self.dav = dav
        self.identity = identity
        self.password = password or None
        self.encryption_expected = bool(password) and not allow_plaintext
        self.tz = tz
        self.cache_ttl = cache_ttl
        self.verify_upload = verify_upload
        self.write_backup = write_backup
        self.backup_retention_days = max(1, backup_retention_days)
        self.max_attempts = max(1, max_attempts)
        self._cache: SyncFile | None = None
        self._lock = asyncio.Lock()
        self._last_prune: float | None = None

    @classmethod
    def from_settings(cls, s: Settings) -> SyncStore:
        dav = NextcloudDav(
            s.nextcloud_url,
            s.nextcloud_user,
            s.dav_login,
            s.nextcloud_password.get_secret_value(),
            s.nextcloud_sync_folder,
            timeout=s.http_timeout_seconds,
            max_retries=s.http_max_retries,
        )
        identity = ClientIdentity.load(s.data_dir, s.sp_client_id)
        return cls(
            dav,
            identity,
            password=s.sp_encryption_password.get_secret_value() if s.sp_encryption_password else None,
            allow_plaintext=s.sp_allow_plaintext,
            tz=ZoneInfo(s.sp_timezone),
            cache_ttl=s.sp_cache_ttl_seconds,
            verify_upload=s.sp_verify_upload,
            write_backup=s.sp_write_backup,
            backup_retention_days=s.sp_backup_retention_days,
            max_attempts=s.sp_max_write_attempts,
        )

    # --- reading ---------------------------------------------------------------

    def _decode(self, raw: str, rev: str, strong: str | None) -> SyncFile:
        try:
            flags, data = codec.decode_sync_file(raw, self.password, encryption_expected=self.encryption_expected)
        except codec.CodecError as e:
            raise SyncError(str(e)) from e
        try:
            validate_envelope(data)
        except Exception as e:  # noqa: BLE001
            raise SyncError(str(e)) from e
        return SyncFile(raw=raw, flags=flags, data=data, rev=rev, strong_etag=strong)

    async def _download(self) -> SyncFile:
        try:
            got = await self.dav.get(SYNC_FILE)
        except NotFound as e:
            raise SyncError(
                f"No '{SYNC_FILE}' in the configured sync folder. Run a sync from a Super "
                "Productivity client first; this server never creates the initial file."
            ) from e
        sf = await asyncio.to_thread(self._decode, got.text, got.rev, got.strong_etag)
        self._cache = sf
        return sf

    async def load(self, *, force: bool = False) -> SyncFile:
        cached = self._cache
        if cached is not None and not force:
            age = time.monotonic() - cached.fetched_at
            if age < self.cache_ttl:
                return cached
            try:
                etag = await self.dav.get_etag(SYNC_FILE)
            except Exception as e:  # noqa: BLE001
                log.debug("etag pre-check failed: %s", e)
                etag = None
            if etag is not None and etag == cached.strong_etag:
                cached.fetched_at = time.monotonic()
                return cached
        return await self._download()

    def now_ms(self) -> int:
        return int(time.time() * 1000)

    def context_for(self, sf: SyncFile) -> MutationContext:
        return MutationContext(sf, self.tz, self.now_ms())

    def _encode(self, envelope: dict[str, Any], sf: SyncFile) -> str:
        try:
            text = codec.encode_sync_file(envelope, sf.flags, self.password)
        except codec.CodecError as e:
            raise SyncError(str(e)) from e
        if not text.strip():
            raise SyncError("Refusing to upload an empty sync file")
        return text

    def read_context_for(self, sf: SyncFile) -> MutationContext:
        """Context over the cached snapshot without copying it (read-only tools)."""
        return MutationContext(sf, self.tz, self.now_ms(), copy_state=False)

    # --- writing ---------------------------------------------------------------

    def _build_envelope(self, sf: SyncFile, ctx: MutationContext) -> dict[str, Any]:
        new_sync_version = sf.sync_version + 1
        clock = merge_clocks(sf.vector_clock, {self.identity.client_id: self.identity.counter})
        compact_ops = []
        for pending in ctx.ops:
            clock = increment_clock(clock, self.identity.client_id)
            compact_ops.append(
                build_compact_op(
                    pending,
                    client_id=self.identity.client_id,
                    vector_clock=clock,
                    timestamp_ms=ctx.now_ms,
                    schema_version=sf.schema_version,
                    sync_version=new_sync_version,
                )
            )
        combined = [*sf.recent_ops, *compact_ops]
        merged_ops = combined[-MAX_RECENT_OPS:]
        if len(combined) > len(merged_ops):
            # Normal steady state once the buffer is full (the app trims the same way).
            log.debug("Trimmed %d old op(s) from recentOps", len(combined) - len(merged_ops))
        _strip_local_only_sync_settings(ctx.state)
        envelope: dict[str, Any] = {
            "version": FILE_VERSION,
            "syncVersion": new_sync_version,
            "schemaVersion": sf.schema_version,
            "vectorClock": clock,
            "lastModified": ctx.now_ms,
            "clientId": self.identity.client_id,
            "state": ctx.state,
            "archiveYoung": sf.data.get("archiveYoung"),
            "archiveOld": sf.data.get("archiveOld"),
            "recentOps": merged_ops,
        }
        if merged_ops and "sv" in merged_ops[0]:
            envelope["oldestOpSyncVersion"] = merged_ops[0]["sv"]
        for k in ("archiveYoung", "archiveOld"):
            if envelope[k] is None:
                del envelope[k]
        return envelope

    async def mutate(self, fn: Callable[[MutationContext], T]) -> T:
        async with self._lock:
            last_error: Exception | None = None
            for attempt in range(1, self.max_attempts + 1):
                sf = await self._download()
                ctx = await asyncio.to_thread(self.context_for, sf)
                result = fn(ctx)
                if not ctx.ops:
                    return result
                envelope = self._build_envelope(sf, ctx)
                text = await asyncio.to_thread(self._encode, envelope, sf)

                if self.write_backup:
                    backup_name = backup_file_name(ctx.now_ms, sf.sync_version)
                    try:
                        await self.dav.put(backup_name, sf.raw)
                    except Exception as e:  # noqa: BLE001
                        log.warning("Could not write %s: %s", backup_name, e)

                try:
                    if sf.strong_etag:
                        put_etag = await self.dav.put(SYNC_FILE, text, if_match=sf.strong_etag)
                    else:
                        # No strong etag: best-effort content check like the app.
                        fresh = await self.dav.get(SYNC_FILE)
                        if fresh.rev != sf.rev:
                            raise PreconditionFailed(SYNC_FILE)
                        put_etag = await self.dav.put(SYNC_FILE, text)
                except (PreconditionFailed, Locked) as e:
                    last_error = e
                    log.info(
                        "%s (attempt %d/%d); retrying",
                        "Sync file locked by another client" if isinstance(e, Locked) else "Concurrent write detected",
                        attempt,
                        self.max_attempts,
                    )
                    self._cache = None
                    continue

                if self.verify_upload or (put_etag is None and sf.strong_etag):
                    # Full verification (opt-in), or the server gave no etag on PUT
                    # although it speaks strong etags: re-download to learn the revision.
                    verify = await self.dav.get(SYNC_FILE)
                    if codec.md5_hex(verify.text) != codec.md5_hex(text):
                        last_error = ConflictError("Upload verification failed; remote content differs")
                        log.warning("%s (attempt %d/%d)", last_error, attempt, self.max_attempts)
                        self._cache = None
                        continue
                    rev, strong = verify.rev, verify.strong_etag
                elif put_etag:
                    rev, strong = put_etag, put_etag
                else:
                    rev, strong = codec.md5_hex(text), None

                self.identity.counter = envelope["vectorClock"][self.identity.client_id]
                self.identity.save()
                self._cache = SyncFile(raw=text, flags=sf.flags, data=envelope, rev=rev, strong_etag=strong)
                log.info(
                    "Uploaded %d op(s); syncVersion %d -> %d",
                    len(ctx.ops),
                    sf.sync_version,
                    envelope["syncVersion"],
                )
                if self.write_backup:
                    await self._prune_backups(ctx.now_ms)
                return result
            raise ConflictError(f"Could not write the sync file after {self.max_attempts} attempts: {last_error}")

    async def _prune_backups(self, now_ms: int) -> None:
        """Delete backups older than the retention window; best effort, at most hourly."""
        if self._last_prune is not None and time.monotonic() - self._last_prune < PRUNE_INTERVAL_S:
            return
        self._last_prune = time.monotonic()
        cutoff = datetime.fromtimestamp(now_ms / 1000, UTC) - timedelta(days=self.backup_retention_days)
        try:
            names = await self.dav.list_names()
            stale = [n for n in names if (ts := backup_timestamp(n)) is not None and ts < cutoff]
            for name in sorted(stale):
                await self.dav.delete(name)
        except Exception as e:  # noqa: BLE001
            log.warning("Could not prune old backups: %s", e)
            return
        if stale:
            log.info("Pruned %d backup(s) older than %d day(s)", len(stale), self.backup_retention_days)

    # --- startup -----------------------------------------------------------------

    async def probe(self) -> SyncFile:
        """Fail fast on misconfiguration: credentials, folder, file format, password.

        Raises ``AuthFailed`` / ``SyncError`` for configuration problems and other
        ``WebDavError`` subclasses for connectivity problems; callers decide which
        of those are fatal.
        """
        await self.dav.test_connection()
        return await self.load(force=True)

    async def aclose(self) -> None:
        await self.dav.aclose()
