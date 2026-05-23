import json
import unittest

import requests

from booklooker_client import BooklookerClient, RateLimiter


def make_response(payload, status_code=200, headers=None):
    response = requests.Response()
    response.status_code = status_code
    response.headers.update(headers or {})
    response._content = json.dumps(payload).encode("utf-8")
    response.encoding = "utf-8"
    return response


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class BooklookerClientTests(unittest.TestCase):
    def test_authenticate_stores_token_and_normalizes_response(self):
        session = FakeSession([make_response({"status": "OK", "returnValue": "TOKEN"})])
        client = BooklookerClient(
            api_key="test-key",
            session=session,
            rate_limit_per_minute=None,
        )

        result = client.authenticate()

        self.assertTrue(result["ok"])
        self.assertEqual(result["data"], "TOKEN")
        self.assertEqual(client.token, "TOKEN")
        self.assertEqual(session.calls[0]["method"], "POST")
        self.assertEqual(session.calls[0]["url"], "https://api.booklooker.de/2.0/authenticate")
        self.assertEqual(session.calls[0]["params"], {"apiKey": "test-key"})

    def test_authenticated_request_refreshes_expired_token_once(self):
        session = FakeSession(
            [
                make_response({"status": "OK", "returnValue": "TOKEN-1"}),
                make_response({"status": "NOK", "returnValue": "TOKEN_EXPIRED"}),
                make_response({"status": "OK", "returnValue": "TOKEN-2"}),
                make_response({"status": "OK", "returnValue": {"orderId": 123}}),
            ]
        )
        client = BooklookerClient(
            api_key="test-key",
            session=session,
            rate_limit_per_minute=None,
        )

        result = client.get_orders(order_id=123)

        self.assertTrue(result["ok"])
        self.assertEqual(result["data"], {"order_id": 123})
        self.assertEqual(client.token, "TOKEN-2")
        self.assertEqual(session.calls[1]["params"]["token"], "TOKEN-1")
        self.assertEqual(session.calls[3]["params"]["token"], "TOKEN-2")

    def test_retries_transient_http_statuses(self):
        session = FakeSession(
            [
                make_response({"status": "NOK", "returnValue": "SERVER_DOWN"}, status_code=503),
                make_response({"status": "OK", "returnValue": "ACTIVE"}),
            ]
        )
        client = BooklookerClient(
            token="TOKEN",
            session=session,
            rate_limit_per_minute=None,
            backoff_factor=0,
        )

        result = client.get_article_status("A-1")

        self.assertTrue(result["ok"])
        self.assertEqual(result["data"], "ACTIVE")
        self.assertEqual(len(session.calls), 2)

    def test_retries_transient_api_codes(self):
        session = FakeSession(
            [
                make_response({"status": "NOK", "returnValue": "SERVER_DOWN"}),
                make_response({"status": "OK", "returnValue": "SUCCESS"}),
            ]
        )
        client = BooklookerClient(
            token="TOKEN",
            session=session,
            rate_limit_per_minute=None,
            backoff_factor=0,
        )

        result = client.delete_article("A-1")

        self.assertTrue(result["ok"])
        self.assertEqual(result["data"], "SUCCESS")
        self.assertEqual(len(session.calls), 2)

    def test_iter_pages_stops_when_last_page_is_short(self):
        session = FakeSession(
            [
                make_response({"status": "OK", "returnValue": {"items": [{"id": 1}, {"id": 2}]}}),
                make_response({"status": "OK", "returnValue": {"items": [{"id": 3}]}}),
            ]
        )
        client = BooklookerClient(
            token="TOKEN",
            session=session,
            rate_limit_per_minute=None,
        )

        pages = list(
            client.iter_pages(
                "GET",
                "/search",
                items_key="items",
                per_page=2,
            )
        )

        self.assertEqual(len(pages), 2)
        self.assertEqual(session.calls[0]["params"]["page"], 1)
        self.assertEqual(session.calls[0]["params"]["limit"], 2)
        self.assertEqual(session.calls[1]["params"]["page"], 2)

    def test_iter_items_flattens_paginated_items(self):
        session = FakeSession(
            [
                make_response({"status": "OK", "returnValue": {"items": [{"itemId": 1}, {"itemId": 2}]}}),
                make_response({"status": "OK", "returnValue": {"items": []}}),
            ]
        )
        client = BooklookerClient(
            token="TOKEN",
            session=session,
            rate_limit_per_minute=None,
        )

        items = list(
            client.iter_items(
                "GET",
                "/search",
                items_key="items",
                per_page=2,
            )
        )

        self.assertEqual(items, [{"item_id": 1}, {"item_id": 2}])

    def test_rate_limiter_sleeps_until_window_allows_call(self):
        current = {"value": 0.0}
        sleeps = []

        def now():
            return current["value"]

        def sleep(seconds):
            sleeps.append(seconds)
            current["value"] += seconds

        limiter = RateLimiter(max_calls=2, period=10, now=now, sleep=sleep)

        limiter.acquire()
        limiter.acquire()
        limiter.acquire()

        self.assertEqual(sleeps, [10.0])

    def test_normalize_converts_camel_case_keys(self):
        client = BooklookerClient(token="TOKEN", rate_limit_per_minute=None)

        result = client.normalize(
            {
                "status": "OK",
                "returnValue": {
                    "orderId": 1,
                    "orderItems": [{"orderItemId": 2}],
                },
            }
        )

        self.assertEqual(
            result["data"],
            {"order_id": 1, "order_items": [{"order_item_id": 2}]},
        )


if __name__ == "__main__":
    unittest.main()
