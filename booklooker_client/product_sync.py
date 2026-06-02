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
DEFAULT_BENCHMARK_CSV_FILE = PROJECT_ROOT / "data" / "booklooker_benchmark.csv"
DEFAULT_INTERVAL_SECONDS = 3600
DEFAULT_SEARCH_LIMIT = 150
DEFAULT_EXTRA_FIELDS = "All"
DEFAULT_BENCHMARK_SAMPLE_SIZE = 10_000
DEFAULT_BENCHMARK_TOTAL_EANS = 850_000
RESULT_SEPARATOR = "___"


class SearchClient(Protocol):
    def search(self, **params: Any) -> dict[str, Any]:
        """Search Booklooker and return the normalized client payload."""


class CountingSession:
    """Requests session wrapper that counts every HTTP call made by the client."""

    def __init__(self, session: Any) -> None:
        self.session = session
        self.request_count = 0

    def request(self, *args: Any, **kwargs: Any) -> Any:
        self.request_count += 1
        return self.session.request(*args, **kwargs)


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


def first_benchmark_price(product: dict[str, Any]) -> str:
    readable_results = product.get("readable_results")
    if not isinstance(readable_results, list) or not readable_results:
        return ""
    first_result = readable_results[0]
    if not isinstance(first_result, dict):
        return ""
    return str(
        first_result.get("price_plus_shipping") or first_result.get("price") or ""
    )


def benchmark_products(
    client: SearchClient,
    identifiers: list[str],
    csv_file: Path,
    *,
    medium: str = "book",
    limit: int = DEFAULT_SEARCH_LIMIT,
    extra_fields: str | None = DEFAULT_EXTRA_FIELDS,
    try_ean_fallback: bool = True,
    total_eans: int = DEFAULT_BENCHMARK_TOTAL_EANS,
    request_count: Any = None,
    now: Any = time.perf_counter,
) -> dict[str, Any]:
    """Run the normal product lookup pipeline for a sample and write CSV timings."""

    csv_file.parent.mkdir(parents=True, exist_ok=True)
    started_at = now()
    rows: list[dict[str, str]] = []
    found_count = 0
    measured_search_requests = 0
    request_count_before = request_count() if request_count is not None else None

    for identifier in identifiers:
        item_started_at = now()
        product = fetch_product_data(
            client,
            identifier,
            medium=medium,
            limit=limit,
            extra_fields=extra_fields,
            try_ean_fallback=try_ean_fallback,
        )
        elapsed_ms = max(0, round((now() - item_started_at) * 1000))
        measured_search_requests += len(product.get("attempts", []))
        found = product.get("result_count", 0) > 0
        if found:
            found_count += 1
        rows.append(
            {
                "ean": identifier,
                "status": "found" if found else "not_found",
                "prix": first_benchmark_price(product),
                "temps_ms": str(elapsed_ms),
            }
        )

    elapsed_seconds = max(0.0, now() - started_at)
    analyzed_count = len(identifiers)
    not_found_count = analyzed_count - found_count
    success_rate = (found_count / analyzed_count * 100) if analyzed_count else 0.0
    eans_per_hour = (
        analyzed_count / elapsed_seconds * 3600 if elapsed_seconds > 0 else 0.0
    )
    estimated_seconds = (
        total_eans / eans_per_hour * 3600 if eans_per_hour > 0 else 0.0
    )
    if request_count is not None and request_count_before is not None:
        api_requests = request_count() - request_count_before
    else:
        api_requests = measured_search_requests

    with csv_file.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["ean", "status", "prix", "temps_ms"],
        )
        writer.writeheader()
        writer.writerows(rows)

    return {
        "analyzed_count": analyzed_count,
        "found_count": found_count,
        "not_found_count": not_found_count,
        "success_rate": success_rate,
        "api_requests": api_requests,
        "elapsed_seconds": elapsed_seconds,
        "eans_per_hour": eans_per_hour,
        "estimated_seconds": estimated_seconds,
        "csv_file": str(csv_file),
    }


def format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.2f}s"
    rounded = int(round(seconds))
    days, remainder = divmod(rounded, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes, secs = divmod(remainder, 60)
    if days:
        return f"{days}d {hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def print_benchmark_summary(stats: dict[str, Any]) -> None:
    print(f"EAN analysés: {stats['analyzed_count']}")
    print(f"Offres trouvées: {stats['found_count']}")
    print(f"Offres non trouvées: {stats['not_found_count']}")
    print(f"Taux de réussite: {stats['success_rate']:.2f}%")
    print(f"Nombre de requêtes API effectuées: {stats['api_requests']}")
    print(f"Temps total: {format_duration(stats['elapsed_seconds'])}")
    print(f"EAN traités par heure: {stats['eans_per_hour']:.2f}")
    print(
        "Estimation pour parcourir 850 000 EAN: "
        f"{format_duration(stats['estimated_seconds'])}"
    )
    print(f"CSV benchmark: {stats['csv_file']}")


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
        "--api-key",
        help="Booklooker API key. Defaults to BOOKLOOKER_API_KEY.",
    )
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
    parser.add_argument(
        "--benchmark",
        action="store_true",
        help="Run a benchmark on a sample of identifiers and write a CSV report.",
    )
    parser.add_argument(
        "--benchmark-sample-size",
        type=int,
        default=DEFAULT_BENCHMARK_SAMPLE_SIZE,
        help=(
            "Number of identifiers to benchmark. "
            f"Default: {DEFAULT_BENCHMARK_SAMPLE_SIZE}."
        ),
    )
    parser.add_argument(
        "--benchmark-csv",
        type=Path,
        default=DEFAULT_BENCHMARK_CSV_FILE,
        help=(
            "CSV file to write for benchmark results. "
            f"Default: {DEFAULT_BENCHMARK_CSV_FILE}"
        ),
    )
    parser.add_argument(
        "--benchmark-total-eans",
        type=int,
        default=DEFAULT_BENCHMARK_TOTAL_EANS,
        help=(
            "EAN count used for the runtime estimate. "
            f"Default: {DEFAULT_BENCHMARK_TOTAL_EANS}."
        ),
    )
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


def run_benchmark_once(args: argparse.Namespace) -> dict[str, Any]:
    identifiers = load_identifiers(args.isbn_file)[: args.benchmark_sample_size]
    base_client = BooklookerClient(
        api_key=args.api_key,
        base_url=args.base_url,
        timeout=args.timeout,
        rate_limit_per_minute=args.rate_limit_per_minute,
        max_retries=args.max_retries,
    )
    counting_session = CountingSession(base_client.session)
    base_client.session = counting_session
    stats = benchmark_products(
        base_client,
        identifiers,
        args.benchmark_csv,
        medium=args.medium,
        limit=args.limit,
        extra_fields=args.extra_fields or None,
        try_ean_fallback=not args.no_ean_fallback,
        total_eans=args.benchmark_total_eans,
        request_count=lambda: counting_session.request_count,
    )
    print_benchmark_summary(stats)
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.interval_seconds < 1:
        parser.error("--interval-seconds must be at least 1")
    if args.benchmark_sample_size < 1:
        parser.error("--benchmark-sample-size must be at least 1")
    if args.benchmark_total_eans < 1:
        parser.error("--benchmark-total-eans must be at least 1")
    if args.benchmark and args.watch:
        parser.error("--benchmark cannot be combined with --watch")

    if args.benchmark:
        run_benchmark_once(args)
        return 0

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
