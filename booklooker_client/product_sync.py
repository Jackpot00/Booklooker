"""Synchronize product data for ISBN/EAN identifiers."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .client import BooklookerClient, DEFAULT_BASE_URL

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ISBN_FILE = PROJECT_ROOT / "data" / "isbn.txt"
DEFAULT_OUTPUT_FILE = PROJECT_ROOT / "data" / "booklooker_products.json"
DEFAULT_INTERVAL_SECONDS = 3600
DEFAULT_SEARCH_LIMIT = 150
DEFAULT_EXTRA_FIELDS = "All"


class SearchClient(Protocol):
    def search(self, **params: Any) -> dict[str, Any]:
        """Search Booklooker and return the normalized client payload."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_identifiers(path: Path) -> list[str]:
    """Load ISBN/EAN values, ignoring empty lines and comments."""

    identifiers: list[str] = []
    seen: set[str] = set()

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        value = raw_line.strip()
        if not value or value.startswith("#"):
            continue
        if value not in seen:
            identifiers.append(value)
            seen.add(value)

    return identifiers


def has_product_data(data: Any) -> bool:
    if data is None:
        return False
    if isinstance(data, str):
        return bool(data.strip())
    if isinstance(data, (list, tuple, set, dict)):
        return bool(data)
    return True


def fetch_product_data(
    client: SearchClient,
    identifier: str,
    *,
    medium: str = "book",
    limit: int = DEFAULT_SEARCH_LIMIT,
    extra_fields: str | None = DEFAULT_EXTRA_FIELDS,
    try_ean_fallback: bool = True,
) -> dict[str, Any]:
    """Fetch product data for one ISBN/EAN.

    Booklooker documents ``isbn`` for books/audio books and ``ean`` for other
    media. ISBN-13 values are EAN-compatible, so the optional fallback helps when
    an identifier is catalogued under EAN instead of ISBN.
    """

    search_keys = ["isbn"]
    if try_ean_fallback:
        search_keys.append("ean")

    attempts: list[dict[str, Any]] = []

    for search_key in search_keys:
        params: dict[str, Any] = {
            "medium": medium,
            "limit": limit,
            search_key: identifier,
        }
        if extra_fields:
            params["extraFields"] = extra_fields

        response = client.search(**params)
        attempts.append(
            {
                "search_key": search_key,
                "request_params": params,
                "response": response,
            }
        )
        if response.get("ok") and has_product_data(response.get("data")):
            break

    best_attempt = attempts[-1]
    for attempt in attempts:
        response = attempt["response"]
        if response.get("ok") and has_product_data(response.get("data")):
            best_attempt = attempt
            break

    response = best_attempt["response"]
    return {
        "identifier": identifier,
        "search_key": best_attempt["search_key"],
        "request_params": best_attempt["request_params"],
        "fetched_at": utc_now(),
        "ok": response.get("ok", False),
        "status": response.get("status"),
        "error": response.get("error"),
        "data": response.get("data"),
        "raw": response.get("raw"),
        "attempts": [
            {
                "search_key": attempt["search_key"],
                "ok": attempt["response"].get("ok", False),
                "status": attempt["response"].get("status"),
                "error": attempt["response"].get("error"),
            }
            for attempt in attempts
        ],
    }


def build_snapshot(
    client: SearchClient,
    identifiers: list[str],
    *,
    medium: str = "book",
    limit: int = DEFAULT_SEARCH_LIMIT,
    extra_fields: str | None = DEFAULT_EXTRA_FIELDS,
    try_ean_fallback: bool = True,
) -> dict[str, Any]:
    products = [
        fetch_product_data(
            client,
            identifier,
            medium=medium,
            limit=limit,
            extra_fields=extra_fields,
            try_ean_fallback=try_ean_fallback,
        )
        for identifier in identifiers
    ]
    return {
        "generated_at": utc_now(),
        "source": {
            "api": "booklooker REST API v2.0",
            "medium": medium,
            "identifier_count": len(identifiers),
            "search_limit": limit,
            "extra_fields": extra_fields,
        },
        "products": products,
    }


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(f"{path.suffix}.tmp")
    tmp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp_path.replace(path)


def sync_products(
    client: SearchClient,
    isbn_file: Path = DEFAULT_ISBN_FILE,
    output_file: Path = DEFAULT_OUTPUT_FILE,
    *,
    medium: str = "book",
    limit: int = DEFAULT_SEARCH_LIMIT,
    extra_fields: str | None = DEFAULT_EXTRA_FIELDS,
    try_ean_fallback: bool = True,
) -> dict[str, Any]:
    identifiers = load_identifiers(isbn_file)
    snapshot = build_snapshot(
        client,
        identifiers,
        medium=medium,
        limit=limit,
        extra_fields=extra_fields,
        try_ean_fallback=try_ean_fallback,
    )
    write_json_atomic(output_file, snapshot)
    return snapshot


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fetch Booklooker product data for ISBN/EAN values."
    )
    parser.add_argument(
        "--isbn-file",
        type=Path,
        default=DEFAULT_ISBN_FILE,
        help=f"File containing one ISBN/EAN per line. Default: {DEFAULT_ISBN_FILE}",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_FILE,
        help=f"JSON file to write. Default: {DEFAULT_OUTPUT_FILE}",
    )
    parser.add_argument("--api-key", help="Booklooker API key. Defaults to BOOKLOOKER_API_KEY.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--medium", default="book", help="Booklooker search medium.")
    parser.add_argument("--limit", type=int, default=DEFAULT_SEARCH_LIMIT)
    parser.add_argument("--extra-fields", default=DEFAULT_EXTRA_FIELDS)
    parser.add_argument(
        "--no-ean-fallback",
        action="store_true",
        help="Do not retry an empty ISBN search with the EAN parameter.",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Keep running and update the JSON file every interval.",
    )
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=DEFAULT_INTERVAL_SECONDS,
        help="Refresh interval used with --watch. Default: 3600.",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--rate-limit-per-minute", type=int, default=100)
    parser.add_argument("--max-retries", type=int, default=3)
    return parser


def run_once(args: argparse.Namespace) -> dict[str, Any]:
    client = BooklookerClient(
        api_key=args.api_key,
        base_url=args.base_url,
        timeout=args.timeout,
        rate_limit_per_minute=args.rate_limit_per_minute,
        max_retries=args.max_retries,
    )
    snapshot = sync_products(
        client,
        args.isbn_file,
        args.output,
        medium=args.medium,
        limit=args.limit,
        extra_fields=args.extra_fields or None,
        try_ean_fallback=not args.no_ean_fallback,
    )
    print(
        f"Wrote {len(snapshot['products'])} products to {args.output}",
        file=sys.stderr,
    )
    return snapshot


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.interval_seconds < 1:
        parser.error("--interval-seconds must be at least 1")

    if not args.watch:
        run_once(args)
        return 0

    while True:
        try:
            run_once(args)
        except Exception as exc:  # pragma: no cover - keeps long-running sync alive.
            print(f"Booklooker product sync failed: {exc}", file=sys.stderr)
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
