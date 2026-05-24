# Booklooker

Python client for the Booklooker REST API v2.0.

## Features

- Uses `requests`
- API-key authentication with automatic token reuse
- One-shot token refresh when Booklooker returns `TOKEN_EXPIRED`, `TOKEN_MISSING`, or `TOKEN_UNKNOWN`
- Retry logic for transient HTTP statuses and Booklooker transient API codes
- Local rate limiting, defaulting to Booklooker's documented 100 requests/minute REST limit
- Configurable pagination helpers
- Normalized JSON responses:
  - `status`
  - `ok`
  - `data` (`returnValue` with nested keys converted to `snake_case`)
  - `error`
  - `raw`

## Installation

```bash
python3 -m pip install .
```

## Usage

Set your API key outside the codebase:

```bash
export BOOKLOOKER_API_KEY="your-api-key"
```

Then call the API:

```python
from booklooker_client import BooklookerClient

client = BooklookerClient()

orders = client.get_orders(date="2026-05-23")
if orders["ok"]:
    print(orders["data"])
else:
    print("Booklooker error:", orders["error"])
```

You can also pass the key directly at runtime:

```python
client = BooklookerClient(api_key="your-api-key")
```

## Pagination

Booklooker's documented REST v2.0 endpoints generally return bounded result sets
rather than a shared pagination envelope. For endpoints or account-specific
interfaces that support page-style parameters, use `iter_pages` or `iter_items`:

```python
for item in client.iter_items(
    "GET",
    "/search",
    params={"title": "python"},
    items_key="items",
    page_param="page",
    per_page_param="limit",
    per_page=50,
):
    print(item)
```

## Hourly ISBN/EAN product sync

The repository includes `data/isbn.txt` with the requested ISBN values:

```text
9783608942286
9783525516805
9783421046161
9783937715391
9783406384936
```

Generate a normalized JSON snapshot once:

```bash
BOOKLOOKER_API_KEY="your-api-key" python3 scripts/update_booklooker_products.py
```

The default output is:

```text
data/booklooker_products.json
```

Keep the script running and refresh that JSON file every hour:

```bash
BOOKLOOKER_API_KEY="your-api-key" python3 scripts/update_booklooker_products.py --watch
```

The script searches each identifier with Booklooker's `isbn` parameter first and
falls back to `ean` when no ISBN result is returned. It requests
`extraFields=All` by default so Booklooker returns the fullest available product
data for the account/API quota.

## Supported endpoint helpers

- `authenticate`
- `get_article_list`
- `get_article_status`
- `delete_article`
- `delete_image`
- `search`
- `import_file`
- `get_file_status`
- `get_import_status`
- `get_orders`
- `cancel_order`
- `cancel_order_item`
- `send_order_message`
- `set_order_status`
- `request` for arbitrary endpoints

## Testing

```bash
python3 -m unittest discover -s tests
```
