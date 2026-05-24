import json
import tempfile
import unittest
from pathlib import Path

from booklooker_client.product_sync import (
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


if __name__ == "__main__":
    unittest.main()
