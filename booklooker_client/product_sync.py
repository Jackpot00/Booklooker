"""Synchronize product data for ISBN/EAN identifiers."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

from .client import BooklookerClient, DEFAULT_BASE_URL

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ISBN_FILE = PROJECT_ROOT / "data" / "isbn.txt"
DEFAULT_OUTPUT_FILE = PROJECT_ROOT / "data" / "booklooker_products.json"
DEFAULT_RESULTS_CSV_FILE = PROJECT_ROOT / "resultats.csv"
DEFAULT_INTERVAL_SECONDS = 3600
DEFAULT_SEARCH_LIMIT = 150
DEFAULT_EXTRA_FIELDS = "All"
RESULT_SEPARATOR = "___"
RESULTS_CSV_FIELDS = ["isbn", "status", "prix_booklooker", "duree_ms"]


class SearchClient(Protocol):
    def search(self, **params: Any) -> dict[str, Any]:
        """Search Booklooker and return the normalized client payload."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_results_csv(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return

    with path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=RESULTS_CSV_FIELDS)
        writer.writeheader()


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
    offers = extract_offers(data)
    if offers:
        return True
    parsed = parse_search_data(data)
    if isinstance(parsed, dict) and any(isinstance(value, list) for value in parsed.values()):
        return False
    if isinstance(parsed, list):
        return bool(parsed)
    if data is None:
        return False
    if isinstance(data, str):
        return bool(data.strip())
    if isinstance(data, (list, tuple, set, dict)):
        return bool(data)
    return True


def parse_search_data(data: Any) -> Any:
    """Parse and normalize Booklooker search data.

    The search endpoint returns JSON as a string inside ``returnValue``. Keeping
    it parsed makes the snapshot readable and queryable.
    """

    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return data
    return normalize_keys(data)


def normalize_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            to_snake_case(str(key)): normalize_keys(nested)
            for key, nested in value.items()
        }
    if isinstance(value, list):
        return [normalize_keys(item) for item in value]
    return value


def to_snake_case(value: str) -> str:
    value = value.replace("-", "_")
    value = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", value)
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    return value.lower()


def extract_offers(data: Any) -> list[dict[str, Any]]:
    parsed = parse_search_data(data)
    if isinstance(parsed, list):
        return [offer for offer in parsed if isinstance(offer, dict)]
    if not isinstance(parsed, dict):
        return []

    for key in ("book", "abook", "film", "music", "game"):
        value = parsed.get(key)
        if isinstance(value, list):
            return [offer for offer in value if isinstance(offer, dict)]

    for value in parsed.values():
        if isinstance(value, list):
            return [offer for offer in value if isinstance(offer, dict)]
    return []


def parse_money(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).replace(",", "."))
    except (InvalidOperation, ValueError):
        return None


def format_money(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return f"{value.quantize(Decimal('0.01'))}"


def price_plus_shipping(offer: dict[str, Any]) -> str | None:
    price = parse_money(offer.get("price"))
    shipping = parse_money(offer.get("shipping_price"))
    if price is None and shipping is None:
        return None
    return format_money((price or Decimal("0")) + (shipping or Decimal("0")))


def clean_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return " ".join(str(value).split())


def build_readable_result(
    identifier: str, offer: dict[str, Any], index: int
) -> dict[str, Any]:
    ean = offer.get("ean") or offer.get("isbn") or identifier
    condition = offer.get("condition")
    if condition is None and offer.get("new") == 1:
        condition = "Neuware"

    result = {
        "result_number": index,
        "separator": RESULT_SEPARATOR,
        "ean": ean,
        "title": offer.get("title"),
        "price": format_money(parse_money(offer.get("price"))),
        "shipping_price": format_money(parse_money(offer.get("shipping_price"))),
        "price_plus_shipping": price_plus_shipping(offer),
        "currency": "EUR",
        "location": offer.get("seller_country") or offer.get("country"),
        "seller_name": offer.get("offerer"),
        "condition": condition,
        "comment": clean_text(offer.get("infotext")),
        "detail_link_url": offer.get("detail_link_url"),
    }
    result["display"] = format_readable_result(result)
    return result


def format_readable_result(result: dict[str, Any]) -> str:
    lines = [
        f"EAN : {result.get('ean') or 'n/a'}",
        f"Prix + Shipping : {result.get('price_plus_shipping') or 'n/a'} EUR",
        f"Localisation : {result.get('location') or 'n/a'}",
        f"Nom du vendeur : {result.get('seller_name') or 'n/a'}",
        f"Etat : {result.get('condition') or 'n/a'}",
        f"Commentaire : {result.get('comment') or 'n/a'}",
    ]
    return "\n".join(lines + [RESULT_SEPARATOR])


def build_readable_results(identifier: str, data: Any) -> list[dict[str, Any]]:
    return [
        build_readable_result(identifier, offer, index)
        for index, offer in enumerate(extract_offers(data), start=1)
    ]


def product_status(product: dict[str, Any]) -> str:
    if product.get("ok") and product.get("result_count", 0) > 0:
        return "FOUND"
    return "NOT_FOUND"


def product_price(product: dict[str, Any]) -> str:
    readable_results = product.get("readable_results")
    if not isinstance(readable_results, list) or not readable_results:
        return ""

    first_result = readable_results[0]
    if not isinstance(first_result, dict):
        return ""
    return str(first_result.get("price") or first_result.get("price_plus_shipping") or "")


def append_results_csv_row(
    path: Path, product: dict[str, Any], duration_ms: int
) -> None:
    with path.open("a", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=RESULTS_CSV_FIELDS)
        writer.writerow(
            {
                "isbn": product.get("identifier", ""),
                "status": product_status(product),
                "prix_booklooker": product_price(product),
                "duree_ms": duration_ms,
            }
        )


def build_performance_summary(
    products: list[dict[str, Any]], elapsed_seconds: float
) -> dict[str, Any]:
    total = len(products)
    found = sum(1 for product in products if product_status(product) == "FOUND")
    not_found = total - found
    success_rate = (found / total * 100) if total else 0.0
    isbn_per_hour = (total / elapsed_seconds * 3600) if elapsed_seconds > 0 else 0.0

    return {
        "total": total,
        "found": found,
        "not_found": not_found,
        "success_rate": success_rate,
        "elapsed_seconds": elapsed_seconds,
        "isbn_per_hour": isbn_per_hour,
    }


def print_performance_summary(summary: dict[str, Any]) -> None:
    print(f"Total ISBN traites: {summary['total']}", file=sys.stderr)
    print(f"Trouves: {summary['found']}", file=sys.stderr)
    print(f"Introuvables: {summary['not_found']}", file=sys.stderr)
    print(f"Taux de reussite: {summary['success_rate']:.2f}%", file=sys.stderr)
    print(f"Temps total d'execution: {summary['elapsed_seconds']:.2f} s", file=sys.stderr)
    print(f"ISBN traites par heure: {summary['isbn_per_hour']:.2f}", file=sys.stderr)


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
    parsed_data = parse_search_data(response.get("data"))
    readable_results = build_readable_results(identifier, parsed_data)
    return {
        "identifier": identifier,
        "search_key": best_attempt["search_key"],
        "request_params": best_attempt["request_params"],
        "fetched_at": utc_now(),
        "ok": response.get("ok", False),
        "status": response.get("status"),
        "error": response.get("error"),
        "result_count": len(readable_results),
        "readable_results": readable_results,
        "readable_text": "\n".join(result["display"] for result in readable_results),
        "data": parsed_data,
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
    results_csv_file: Path | None = None,
) -> dict[str, Any]:
    products = []
    for identifier in identifiers:
        started_at = time.perf_counter()
        product = fetch_product_data(
            client,
            identifier,
            medium=medium,
            limit=limit,
            extra_fields=extra_fields,
            try_ean_fallback=try_ean_fallback,
        )
        duration_ms = int(round((time.perf_counter() - started_at) * 1000))
        products.append(product)
        if results_csv_file is not None:
            append_results_csv_row(results_csv_file, product, duration_ms)

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
    results_csv_file: Path | None = DEFAULT_RESULTS_CSV_FILE,
    *,
    medium: str = "book",
    limit: int = DEFAULT_SEARCH_LIMIT,
    extra_fields: str | None = DEFAULT_EXTRA_FIELDS,
    try_ean_fallback: bool = True,
) -> dict[str, Any]:
    identifiers = load_identifiers(isbn_file)
    if results_csv_file is not None:
        ensure_results_csv(results_csv_file)
    snapshot = build_snapshot(
        client,
        identifiers,
        medium=medium,
        limit=limit,
        extra_fields=extra_fields,
        try_ean_fallback=try_ean_fallback,
        results_csv_file=results_csv_file,
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
    parser.add_argument(
        "--results-csv",
        type=Path,
        default=DEFAULT_RESULTS_CSV_FILE,
        help=f"CSV performance log to append. Default: {DEFAULT_RESULTS_CSV_FILE}",
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
    started_at = time.perf_counter()
    snapshot = sync_products(
        client,
        args.isbn_file,
        args.output,
        args.results_csv,
        medium=args.medium,
        limit=args.limit,
        extra_fields=args.extra_fields or None,
        try_ean_fallback=not args.no_ean_fallback,
    )
    elapsed_seconds = time.perf_counter() - started_at
    print(
        f"Wrote {len(snapshot['products'])} products to {args.output}",
        file=sys.stderr,
    )
    print_performance_summary(
        build_performance_summary(snapshot["products"], elapsed_seconds)
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
