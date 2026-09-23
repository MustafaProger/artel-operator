"""Exact client identity matching for temporary, explicit report exclusions."""
from __future__ import annotations

import re

from .calculator import company_identity


def validate_excluded_clients(exclusions: object) -> None:
    """Require a stable portal ID and a full name for offline source matching."""
    if not isinstance(exclusions, list):
        raise ValueError("excluded_clients должен быть списком объектов {id, name}")
    seen = set()
    for entry in exclusions:
        if not isinstance(entry, dict) or set(entry) != {"id", "name"}:
            raise ValueError("Каждое исключение excluded_clients должно содержать только id и name")
        client_id, name = entry["id"], entry["name"]
        if not isinstance(client_id, str) or not re.fullmatch(r"[1-9]\d*", client_id):
            raise ValueError("excluded_clients.id должен быть строкой с положительным числовым ID GloPro")
        if not isinstance(name, str) or not any(char.isalnum() for char in name) or any(char in name for char in "*?"):
            raise ValueError("excluded_clients.name должен быть полным непустым названием без шаблонов")
        if client_id in seen:
            raise ValueError("excluded_clients содержит повторяющийся ID")
        seen.add(client_id)


def client_is_excluded(client: dict, exclusions: list[dict]) -> bool:
    """Portal IDs take precedence; names only match sources without an ID.

    Keep the legal entity form and compare the whole normalized name. A firm
    with another ID must never disappear merely because its name is identical.
    """
    client_id = client.get("id")
    if client_id is None or client_id == "":
        client_id = client.get("client_id")
    if client_id is not None and client_id != "":
        return any(str(client_id) == entry["id"] for entry in exclusions)
    name = client.get("name") or client.get("client") or ""
    if not name:
        return False
    identity = company_identity(name)
    return any(identity == company_identity(entry["name"]) for entry in exclusions)
