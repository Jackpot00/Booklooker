import json
import tempfile
import unittest
from pathlib import Path

from booklooker_client.product_sync import (
    benchmark_products,
    fetch_product_data,
    load_identifiers,
    sync_products,
)


class FakeSearchClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def search(self, **params):
        self.calls.append(params)
        return self.responses.pop(0)


def response(data, ok=True, status="OK", error=None):
    return {
        "ok": ok,
        "status": status,
        "error": error,
        "data": data,
        "raw": {"status": status, "return_value": data},
    }


class ProductSyncTests(unittest.TestCase):
    def test_load_identifiers_trims_ignores_comments_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "isbn.txt"
            path.write_text(
                "\n# comment\n 9783608942286 \n9783608942286\n9783525516805\n",
                encoding="utf-8",
            )

            identifiers = load_identifiers(path)

        self.assertEqual(identifiers, ["9783608942286", "9783525516805"])

    def test_fetch_product_data_falls_back_from_isbn_to_ean_when_empty(self):
        client = FakeSearchClient(
            [
                response([]),
                response([{"title": "Example", "seller_id": 123}]),
            ]
        )

        product = fetch_product_data(client, "9783608942286")

        self.assertEqual(product["search_key"], "ean")
        self.assertTrue(product["ok"])
        self.assertEqual(product["data"], [{"title": "Example", "seller_id": 123}])
        self.assertEqual(client.calls[0]["isbn"], "9783608942286")
        self.assertEqual(client.calls[1]["ean"], "9783608942286")
        self.assertEqual(product["attempts"][0]["search_key"], "isbn")
        self.assertEqual(product["attempts"][1]["search_key"], "ean")

    def test_fetch_product_data_parses_search_json_and_adds_readable_results(self):
        search_payload = json.dumps(
            {
                "Book": [
                    {
                        "ISBN": "9783608942286",
                        "Title": "Livia",
                        "Price": "15.00",
                        "ShippingPrice": "6.50",
                        "SellerCountry": "DE",
                        "Offerer": "Roxana2003",
                        "Condition": "wie neu",
                        "Infotext": "Wird versichert versendet.",
                    }
                ]
            }
        )
        client = FakeSearchClient([response(search_payload)])

        product = fetch_product_data(client, "9783608942286")

        self.assertEqual(product["result_count"], 1)
        self.assertEqual(product["data"]["book"][0]["shipping_price"], "6.50")
        readable = product["readable_results"][0]
        self.assertEqual(readable["ean"], "9783608942286")
        self.assertEqual(readable["price_plus_shipping"], "21.50")
        self.assertEqual(readable["location"], "DE")
        self.assertEqual(readable["seller_name"], "Roxana2003")
        self.assertEqual(readable["condition"], "wie neu")
        self.assertEqual(readable["comment"], "Wird versichert versendet.")
        self.assertIn("___", readable["display"])

    def test_sync_products_writes_normalized_json_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            isbn_file = tmp_path / "isbn.txt"
            output_file = tmp_path / "products.json"
            isbn_file.write_text("9783608942286\n9783525516805\n", encoding="utf-8")
            client = FakeSearchClient(
                [
                    response([{"title": "First"}]),
                    response([{"title": "Second"}]),
                ]
            )

            snapshot = sync_products(
                client,
                isbn_file,
                output_file,
                try_ean_fallback=False,
            )
            written = json.loads(output_file.read_text(encoding="utf-8"))

        self.assertEqual(snapshot["source"]["identifier_count"], 2)
        self.assertEqual(written["products"][0]["identifier"], "9783608942286")
        self.assertEqual(written["products"][1]["data"], [{"title": "Second"}])
        self.assertEqual(client.calls[0]["extraFields"], "All")
        self.assertEqual(client.calls[0]["limit"], 150)

    def test_benchmark_products_writes_csv_and_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_file = Path(tmp) / "benchmark.csv"
            client = FakeSearchClient(
                [
                    response(
                        json.dumps(
                            {
                                "Book": [
                                    {
                                        "ISBN": "9783608942286",
                                        "Price": "10.00",
                                        "ShippingPrice": "2.50",
                                    }
                                ]
                            }
                        )
                    ),
                    response([]),
                ]
            )
            times = iter([0.0, 0.0, 0.25, 0.25, 0.75, 1.0])

            stats = benchmark_products(
                client,
                ["9783608942286", "9783525516805"],
                csv_file,
                try_ean_fallback=False,
                total_eans=4,
                now=lambda: next(times),
            )
            rows = csv_file.read_text(encoding="utf-8").splitlines()

        self.assertEqual(rows[0], "ean,status,prix,temps_ms")
        self.assertEqual(rows[1], "9783608942286,found,12.50,250")
        self.assertEqual(rows[2], "9783525516805,not_found,,500")
        self.assertEqual(stats["analyzed_count"], 2)
        self.assertEqual(stats["found_count"], 1)
        self.assertEqual(stats["not_found_count"], 1)
        self.assertEqual(stats["api_requests"], 2)
        self.assertEqual(stats["success_rate"], 50.0)
        self.assertEqual(stats["eans_per_hour"], 7200.0)
        self.assertEqual(stats["estimated_seconds"], 2.0)

    def test_benchmark_products_uses_request_counter_when_provided(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_file = Path(tmp) / "benchmark.csv"
            client = FakeSearchClient([response([]), response([])])
            request_values = iter([5, 8])

            stats = benchmark_products(
                client,
                ["9783608942286"],
                csv_file,
                request_count=lambda: next(request_values),
                now=lambda: 1.0,
            )

        self.assertEqual(stats["api_requests"], 3)


if __name__ == "__main__":
    unittest.main()
