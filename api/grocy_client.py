"""Minimal Grocy REST client (ADR-0032).

Reads GROCY_URL / GROCY_API_KEY from the environment (api/secrets.enc.env).
Grocy's API key grants the full API, so nothing here needs the web password.
"""

import os

import requests


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is not set — run via sops exec-env secrets.enc.env")
    return value


URL = _env("GROCY_URL").rstrip("/")
_session = requests.Session()
_session.headers.update({"GROCY-API-KEY": _env("GROCY_API_KEY"), "Accept": "application/json"})


def _req(method: str, path: str, **kwargs):
    r = _session.request(method, f"{URL}/api{path}", timeout=30, **kwargs)
    if r.status_code >= 400:
        raise RuntimeError(f"Grocy {method} {path} -> {r.status_code}: {r.text[:200]}")
    return r.json() if r.content else None


def objects(entity: str) -> list[dict]:
    return _req("GET", f"/objects/{entity}")


def create(entity: str, data: dict) -> int:
    return int(_req("POST", f"/objects/{entity}", json=data)["created_object_id"])


def stock() -> list[dict]:
    return _req("GET", "/stock")


def set_stock(product_id: int, amount: float, location_id: int, note: str = "") -> None:
    """Initial/absolute stock (an inventory correction), not a purchase."""
    _req("POST", f"/stock/products/{product_id}/inventory",
         json={"new_amount": amount, "location_id": location_id, "note": note})


def product_stock(product_id: int) -> float:
    return float(_req("GET", f"/stock/products/{product_id}")["stock_amount"])


def consume(product_id: int, amount: float) -> None:
    _req("POST", f"/stock/products/{product_id}/consume", json={"amount": amount, "transaction_type": "consume", "spoiled": False})


def add_stock(product_id: int, amount: float) -> None:
    _req("POST", f"/stock/products/{product_id}/add", json={"amount": amount, "transaction_type": "purchase"})


def shopping_add(product_id: int, amount: float, list_id: int = 1) -> None:
    _req("POST", "/stock/shoppinglist/add-product", json={"product_id": product_id, "list_id": list_id, "product_amount": amount})


def shopping_list() -> list[dict]:
    return _req("GET", "/objects/shopping_list")


def recipe_fulfillment(recipe_id: int) -> dict:
    return _req("GET", f"/recipes/{recipe_id}/fulfillment")


def recipe_shop_missing(recipe_id: int) -> None:
    _req("POST", f"/recipes/{recipe_id}/add-not-fulfilled-products-to-shoppinglist", json={})


def recipe_consume(recipe_id: int) -> None:
    _req("POST", f"/recipes/{recipe_id}/consume", json={})
