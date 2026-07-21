"""
Thin client for the ManageEngine Endpoint Central (EC) REST API — on-prem.

CONFIRMED against the real instance (172.24.104.109):
  - Auth: a static API key works directly as the `Authorization` header
    value on every request. No separate token exchange needed.
  - Endpoint: GET /api/1.4/som/computers
  - Response shape: {"message_response": {"total": N, "limit": N, "page": N,
    "computers": [...]}, "status": "success", ...}
  - `page` works as a query param. `limit` is REJECTED
    (IAM0028 "Unsupported parameter limit"). Page size is fixed at 25.
"""

import os
from dataclasses import dataclass

import requests

EC_BASE_URL = os.environ.get("EC_BASE_URL")
EC_API_KEY = os.environ.get("EC_API_KEY")
EC_API_VERSION = os.environ.get("EC_API_VERSION", "1.4")

REQUEST_TIMEOUT_SECONDS = 30

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
    body = session.get("som", "computers", params={"page": page})
    response = body.get("message_response", {})
    return response.get("computers", []), response.get("total", 0)


def get_all_computers(session: ECSession) -> list[dict]:
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
                f"Page {page} returned no new resource_ids — pagination stopped working."
            )
        seen_ids |= page_ids
        all_computers.extend(computers)
        if len(all_computers) >= total:
            break
        page += 1
    return all_computers