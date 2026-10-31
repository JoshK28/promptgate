"""Resolve lookup ownership from the caller's verified identity."""

import re


_RESOURCE_ID = re.compile(r"^[a-z]{1,4}_[A-Za-z0-9]{1,32}$")


class AccessDenied(Exception):
    """The resource ID is invalid or a public write was requested."""


def resolve_owner(requester_sub: str, resource_id: str, *, write: bool) -> str:
    if _RESOURCE_ID.fullmatch(resource_id) is None:
        raise AccessDenied("Invalid resource ID.")

    if resource_id.startswith("pub_"):
        if write:
            raise AccessDenied("Public resources are read-only.")
        return "public"

    return requester_sub
