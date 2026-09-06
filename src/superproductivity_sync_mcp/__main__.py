"""``superproductivity-sync-mcp`` console entry point."""

from __future__ import annotations

import asyncio
import logging
import sys

import uvicorn

from .app import create_app
from .config import Settings
from .store import SyncError, SyncStore
from .webdav import AuthFailed, NotFound, WebDavError

log = logging.getLogger(__name__)


def _fail(message: str) -> None:
    print(f"Configuration error: {message}", file=sys.stderr)
    sys.exit(2)


async def _probe(store: SyncStore) -> None:
    """Check Nextcloud and the sync file once before serving.

    Wrong credentials, folder, password or file format are configuration
    errors and abort start-up. A Nextcloud that is merely unreachable is
    reported and the server starts anyway (it retries on every tool call).
    """
    try:
        sf = await store.probe()
    except (AuthFailed, NotFound, SyncError) as e:
        _fail(str(e))
    except WebDavError as e:
        log.warning("Nextcloud not reachable at start-up, continuing anyway: %s", e)
        return
    log.info(
        "Connected: sync-data.json syncVersion=%d schemaVersion=%d, %d recent ops, last writer %s",
        sf.sync_version,
        sf.schema_version,
        len(sf.recent_ops),
        sf.data.get("clientId"),
    )


def main() -> None:
    try:
        settings = Settings()  # type: ignore[call-arg]
        settings.validate_runtime()
    except Exception as e:  # noqa: BLE001
        _fail(str(e))
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        store = SyncStore.from_settings(settings)
    except Exception as e:  # noqa: BLE001
        _fail(str(e))
    asyncio.run(_probe(store))
    app = create_app(settings, store=store)
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        # The access log would record /t/<token>/mcp request targets verbatim.
        access_log=False,
        proxy_headers=True,
        forwarded_allow_ips="*",
        timeout_keep_alive=75,
    )


if __name__ == "__main__":
    main()
