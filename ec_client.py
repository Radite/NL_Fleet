"""
Thin client for the ManageEngine Endpoint Central (EC) REST API — on-prem.

--- 2026-07 update: rewritten against a REAL response from the customer's
instance (172.24.104.109), not public docs. Confirmed facts below replace
the earlier guesses; anything still marked VERIFY is still unconfirmed.

CONFIRMED:
  - Auth: a static API key works directly as the `Authorization` header
    value on every request. No separate username/password token exchange
    call is needed — the old authenticate() function (session token via
    /desktop/authentication) has been removed. If this key ever expires or
    stops working, check Admin > API Access in the EC console for whether
    it needs manual regeneration on a schedule.
  - Endpoint: GET /api/1.4/som/computers (NOT /api/1.4/inventory/computers
    — that one requires a hwid and returns hardware-filtered results, not
    a full computer list).
  - Response shape: {"message_response": {"total": N, "limit": N, "page": N,
    "computers": [...]}, "status": "success", ...} — NOT the originally
    guessed {"message": {"computers": [...]}}.
  - total=446, limit=25, page=1 seen on a real call — pagination clearly
    exists server-side.

CONFIRMED (pagination, tested against the real instance):
  - `page` works as a query param — page=2 returned entirely different
    resource_ids than page=1.
  - `limit` is REJECTED outright: {"errorCode": "IAM0028", "errorMsg":
    "Unsupported parameter limit detected in the request."}. Page size is
    fixed server-side at 25 with no way to override it — get_all_computers()
    below just pages through at that fixed size (~18 calls for 446 devices).
"""

import os
from dataclasses import dataclass

import requests

EC_BASE_URL = os.environ.get("EC_BASE_URL")  # e.g. "https://172.24.104.109:8383"
EC_API_KEY = os.environ.get("EC_API_KEY")
EC_API_VERSION = os.environ.get("EC_API_VERSION", "1.4")

REQUEST_TIMEOUT_SECONDS = 30

# On-prem EC instances typically run self-signed certs. Verifying against
# the real cert (rather than disabling verification) is the safer option —
# set EC_CA_BUNDLE to a path to the server's cert/chain if you have it.
# Falling back to no verification is a real security tradeoff (defeats
# TLS's protection against a MITM on your network), not a formality — only
# do it if you've accepted that tradeoff deliberately.
EC_CA_BUNDLE = os.environ.get("EC_CA_BUNDLE")
VERIFY = EC_CA_BUNDLE if EC_CA_BUNDLE else False


class ECAuthError(RuntimeError):
    pass


@dataclass
class ECSession:
    base_url: str
    api_version: str
    api_key: str

    def _url(self, entity: str, operation: str) -> str:
        return f"{self.base_url}/api/{self.api_version}/{entity}/{operation}"

    def get(self, entity: str, operation: str, params: dict | None = None) -> dict:
        headers = {"Authorization": self.api_key}
        resp = requests.get(
            self._url(entity, operation),
            headers=headers,
            params=params or {},
            timeout=REQUEST_TIMEOUT_SECONDS,
            verify=VERIFY,
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("status") == "error":
            raise ECAuthError(
                f"EC API returned an error: {body.get('error_code')} — "
                f"{body.get('error_description')}"
            )
        return body


def get_session() -> ECSession:
    if not (EC_BASE_URL and EC_API_KEY):
        raise ECAuthError("EC_BASE_URL and EC_API_KEY must both be set.")
    return ECSession(base_url=EC_BASE_URL, api_version=EC_API_VERSION, api_key=EC_API_KEY)


def get_computers(session: ECSession, page: int = 1) -> tuple[list[dict], int]:
    """Fetches one page of computer records via SoM Computers.

    CONFIRMED against the real instance: `page` works and returns distinct
    resource_ids per page. `limit` is REJECTED by the server
    (IAM0028 "Unsupported parameter limit detected in the request") — page
    size is fixed server-side at 25 with no way to override it. Don't pass
    it.

    Returns (computers, total) so the caller knows when to stop paginating.
    """
    body = session.get("som", "computers", params={"page": page})
    response = body.get("message_response", {})
    return response.get("computers", []), response.get("total", 0)


def get_all_computers(session: ECSession) -> list[dict]:
    """Paginates through the full computer list at the server's fixed page
    size (25/page — see get_computers docstring). Stops when a page returns
    the same resource_ids as the previous page, as a defensive guard against
    any future change in pagination behavior."""
    all_computers = []
    seen_ids = set()
    page = 1
    while True:
        computers, total = get_computers(session, page=page)
        if not computers:
            break
        page_ids = {c.get("resource_id") for c in computers}
        if page_ids and page_ids.issubset(seen_ids):
            raise RuntimeError(
                f"Page {page} returned no new resource_ids — pagination via "
                "the page query param may have stopped working. Investigate "
                "before trusting this sync run."
            )
        seen_ids |= page_ids
        all_computers.extend(computers)
        if len(all_computers) >= total:
            break
        page += 1
    return all_computers
