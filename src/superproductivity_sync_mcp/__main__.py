"""``superproductivity-sync-mcp`` console entry point."""

from __future__ import annotations

import logging
import sys

import uvicorn

from .app import create_app
from .config import Settings


def main() -> None:
    try:
        settings = Settings()  # type: ignore[call-arg]
        settings.validate_runtime()
    except Exception as e:  # noqa: BLE001
        print(f"Configuration error: {e}", file=sys.stderr)
        sys.exit(2)
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    app = create_app(settings)
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        proxy_headers=True,
        forwarded_allow_ips="*",
        timeout_keep_alive=75,
    )


if __name__ == "__main__":
    main()
