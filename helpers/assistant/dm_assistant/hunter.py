"""Email lookup through Hunter.io's Email Finder API (https://hunter.io/api-documentation/v2)."""
from __future__ import annotations

import re

import httpx

from .config import settings

_URL = "https://api.hunter.io/v2/email-finder"


class HunterError(RuntimeError):
    pass


def find_email(full_name: str, company: str) -> dict:
    """Returns {email, score, position, domain, sources}. Raises HunterError with a readable message."""
    if not settings.hunter_api_key:
        raise HunterError("HUNTER_API_KEY is not set in .env")
    parts = full_name.split()
    if len(parts) < 2:
        raise HunterError("Enter the person's first and last name.")
    company = company.strip()
    if not company:
        raise HunterError("Enter the company name or its website (e.g. acme.com).")
    params = {"first_name": parts[0], "last_name": " ".join(parts[1:]), "api_key": settings.hunter_api_key}
    if re.fullmatch(r"(https?://)?(www\.)?[\w-]+(\.[\w-]+)+/?.*", company):
        params["domain"] = re.sub(r"^https?://(www\.)?|^www\.|/.*$", "", company)
    else:
        params["company"] = company
    try:
        r = httpx.get(_URL, params=params, timeout=20)
    except httpx.HTTPError as e:
        raise HunterError(f"Couldn't reach Hunter.io: {e.__class__.__name__}") from None
    if r.status_code == 401:
        raise HunterError("Hunter.io rejected the API key.")
    if r.status_code == 429:
        raise HunterError("Hunter.io rate limit or monthly search quota reached.")
    if r.status_code >= 400:
        try:
            detail = r.json()["errors"][0].get("details", "")
        except Exception:
            detail = ""
        raise HunterError(f"Hunter.io error {r.status_code}. {detail}".strip())
    data = r.json().get("data") or {}
    if not data.get("email"):
        raise HunterError("Hunter.io found no email for that person at that company.")
    return {"email": data["email"], "score": data.get("score"), "position": data.get("position") or "",
            "domain": data.get("domain") or params.get("domain", ""),
            "sources": len(data.get("sources") or [])}
