#!/usr/bin/env python3
"""Durable identity and observation helpers for the private job ledger.

The scraper workflow is documented in ``.claude/skills/job-scraper/SKILL.md``.
This module keeps the ledger contract executable and testable without adding a
database, service, or third-party dependency. Callers still own the private
ledger path and decide which observations to pass to :func:`observe`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import html
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


TRACKING_QUERY_KEYS = frozenset(
    {
        "dclid",
        "fbclid",
        "gclid",
        "igshid",
        "li_fat_id",
        "mc_cid",
        "mc_eid",
        "msclkid",
        "twclid",
    }
)

# These are lifecycle values already used by the surrounding workflow or by
# the application tracker. They stay queryable, but are not new candidates by
# default. Unknown future values remain queryable and are not silently hidden.
CLOSED_STATUSES = frozenset(
    {
        "applied",
        "expired",
        "hired",
        "interview",
        "interview only",
        "no response",
        "no_response",
        "offer",
        "offer declined",
        "offer_declined",
        "rejected",
        "withdrawn",
    }
)

_IDENTITY_FIELDS = ("source", "source_id", "canonical_url", "fingerprint")
_MATERIAL_FIELDS = ("title", "company", "location")
_OPTIONAL_MATERIAL_FIELDS = ("content_hash", "description", "snippet")


def _string(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalized_part(value: Any) -> str:
    """Normalize identity text while retaining every word and qualifier."""

    text = html.unescape(unicodedata.normalize("NFKC", _string(value))).casefold()
    chars: list[str] = []
    for char in text:
        category = unicodedata.category(char)
        # Keep symbols that commonly carry meaning in job titles (C++, C#,
        # R&D, and internal dotted names). Separators still normalize to
        # spaces so harmless punctuation/case differences do not duplicate a
        # posting.
        chars.append(char if category[0] in {"L", "N"} or char in "+#&." else " ")
    text = re.sub(r"(?<!\w)\.|\.(?!\w)", " ", "".join(chars))
    return re.sub(r"\s+", " ", text).strip()


def normalize_canonical_url(url: Any) -> str | None:
    """Return a conservative URL comparison value.

    Fragments and well-known click identifiers are removed. Functional query
    parameters are retained and sorted, so a URL is only collapsed when the
    difference is clearly tracking metadata.
    """

    raw = _string(url)
    if not raw:
        return None

    parts = urlsplit(raw)
    if not parts.scheme or not parts.netloc:
        return raw.rstrip("/") or raw

    try:
        hostname = parts.hostname or ""
        port = parts.port
    except ValueError:
        return raw.rstrip("/") or raw

    host = hostname.casefold()
    default_port = (parts.scheme.casefold() == "http" and port == 80) or (
        parts.scheme.casefold() == "https" and port == 443
    )
    netloc = host if not port or default_port else f"{host}:{port}"
    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/") or "/"

    query_pairs = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        key_folded = key.casefold()
        if key_folded.startswith("utm_") or key_folded in TRACKING_QUERY_KEYS:
            continue
        query_pairs.append((key, value))
    query = urlencode(sorted(query_pairs))

    return urlunsplit((parts.scheme.casefold(), netloc, path, query, ""))


def source_name(observation: Mapping[str, Any]) -> str | None:
    value = observation.get("source") or observation.get("portal")
    normalized = _normalized_part(value)
    return normalized or None


def source_id(observation: Mapping[str, Any]) -> str | None:
    for field in ("source_id", "job_id", "id"):
        value = _string(observation.get(field))
        if value:
            return unicodedata.normalize("NFKC", value).casefold()
    return None


def company_title_location_fingerprint(observation: Mapping[str, Any]) -> str | None:
    """Build a conservative fallback identity from all three core fields."""

    parts = tuple(_normalized_part(observation.get(field)) for field in _MATERIAL_FIELDS)
    if not all(parts):
        return None
    return "|".join(parts)


def _identity(observation: Mapping[str, Any]) -> dict[str, str]:
    source = source_name(observation)
    stable_id = source_id(observation)
    canonical_url = normalize_canonical_url(observation.get("url"))
    fingerprint = company_title_location_fingerprint(observation)

    identity: dict[str, str] = {}
    if source:
        identity["source"] = source
    if stable_id:
        identity["source_id"] = stable_id
    if canonical_url:
        identity["canonical_url"] = canonical_url
    if fingerprint:
        identity["fingerprint"] = fingerprint
    return identity


def identity_key(observation: Mapping[str, Any]) -> str:
    """Return the durable key using the strongest available identity."""

    identity = _identity(observation)
    if identity.get("source") and identity.get("source_id"):
        return f"source:{identity['source']}:{identity['source_id']}"
    if identity.get("canonical_url"):
        return f"url:{identity['canonical_url']}"
    if identity.get("fingerprint"):
        return f"fingerprint:{identity['fingerprint']}"
    raise ValueError(
        "job observation needs source/portal plus a stable ID, a URL, "
        "or company, title, and location"
    )


def _material_fingerprint(observation: Mapping[str, Any]) -> tuple[str | None, tuple[str, ...]]:
    values: dict[str, str] = {}
    for field in _MATERIAL_FIELDS:
        value = _normalized_part(observation.get(field))
        if value:
            values[field] = value
    for field in _OPTIONAL_MATERIAL_FIELDS:
        value = _string(observation.get(field))
        if value:
            values[field] = _normalized_part(value)
    if not values:
        return None, ()
    payload = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest(), tuple(values)


def _material_changed(entry: Mapping[str, Any], observation: Mapping[str, Any]) -> bool:
    previous, previous_fields = _material_fingerprint(entry)
    current, current_fields = _material_fingerprint(observation)
    if not previous or not current:
        return False
    # Missing optional detail in a later portal result is not itself a change.
    # Compare only when both observations have the same basis.
    if previous_fields != current_fields:
        return False
    return previous != current


def _entry_identity(entry: Mapping[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    nested = entry.get("identity")
    if isinstance(nested, Mapping):
        for field in _IDENTITY_FIELDS:
            value = _string(nested.get(field))
            if value:
                result[field] = value

    # Additive fields and legacy records remain valid identity sources. URL
    # normalization also lets old records match tracking-tagged URLs.
    result.setdefault("source", source_name(entry) or "")
    legacy_id = source_id(entry)
    if legacy_id:
        result.setdefault("source_id", legacy_id)
    if "canonical_url" not in result:
        canonical = normalize_canonical_url(entry.get("url"))
        if canonical:
            result["canonical_url"] = canonical
    if "fingerprint" not in result:
        fingerprint = company_title_location_fingerprint(entry)
        if fingerprint:
            result["fingerprint"] = fingerprint
    return {key: value for key, value in result.items() if value}


def _find_match(ledger: Mapping[str, Any], identity: Mapping[str, str]) -> str | None:
    seen = ledger.get("seen", {})
    if not isinstance(seen, Mapping):
        return None

    # Once a strong identity is present, never fall all the way back to a
    # fingerprint: two distinct roles can share a company/title/location.
    # Canonical URL remains a compatibility fallback for a legacy entry that
    # predates stored source IDs.
    if identity.get("source") and identity.get("source_id"):
        comparisons = (("source", "source_id"), ("canonical_url",))
    elif identity.get("canonical_url"):
        comparisons = (("canonical_url",),)
    else:
        comparisons = (("fingerprint",),)
    for fields in comparisons:
        if not all(identity.get(field) for field in fields):
            continue
        for key, entry in seen.items():
            if not isinstance(entry, Mapping):
                continue
            existing = _entry_identity(entry)
            if all(existing.get(field) == identity.get(field) for field in fields):
                return str(key)
    return None


def _set_identity(entry: dict[str, Any], identity: Mapping[str, str]) -> None:
    existing = entry.get("identity")
    merged = dict(existing) if isinstance(existing, Mapping) else {}
    for field in _IDENTITY_FIELDS:
        value = identity.get(field)
        if value:
            merged[field] = value
    if merged:
        entry["identity"] = merged


def _status(value: Any) -> str:
    return _string(value).casefold()


def _new_entry(
    observation: Mapping[str, Any],
    identity: Mapping[str, str],
    observed_on: str,
) -> dict[str, Any]:
    entry = dict(observation)
    entry.setdefault("status", "new")
    entry.setdefault("first_seen", observed_on)
    entry["last_seen"] = observed_on
    entry["observation_count"] = 1
    _set_identity(entry, identity)
    material, fields = _material_fingerprint(observation)
    if material:
        entry["material_fingerprint"] = material
        entry["material_fields"] = list(fields)
    return entry


def _unique_key(seen: Mapping[str, Any], base: str) -> str:
    if base not in seen:
        return base
    suffix = 2
    while f"{base}:{suffix}" in seen:
        suffix += 1
    return f"{base}:{suffix}"


@dataclass(frozen=True)
class ObservationResult:
    key: str
    created: bool
    resurfaced: bool
    material_changed: bool
    status: str

    @property
    def is_new_candidate(self) -> bool:
        return _status(self.status) == "new"


def observe(
    ledger: dict[str, Any],
    observation: Mapping[str, Any],
    *,
    observed_on: str | None = None,
    disposition: str | None = None,
    surface_repost: bool = False,
) -> ObservationResult:
    """Merge one observation into a ledger, preserving history and metadata.

    Known jobs update ``last_seen`` and current posting fields. A known closed
    job stays closed when a later ordinary scrape reports ``status: new``. Set
    ``surface_repost`` when the caller has deliberately confirmed a repost or
    material refresh; that creates one new version while retaining the prior
    record for queries.
    """

    if not isinstance(ledger, dict):
        raise TypeError("ledger must be a dictionary")
    seen = ledger.setdefault("seen", {})
    if not isinstance(seen, dict):
        raise ValueError("ledger['seen'] must be an object")

    identity = _identity(observation)
    base_key = identity_key(observation)
    day = observed_on or date.today().isoformat()
    matched_key = _find_match(ledger, identity)

    if matched_key is None:
        key = _unique_key(seen, base_key)
        seen[key] = _new_entry(observation, identity, day)
        return ObservationResult(key, True, False, False, _string(seen[key]["status"]))

    existing = seen[matched_key]
    if not isinstance(existing, dict):
        raise ValueError(f"ledger entry {matched_key!r} must be an object")
    material_changed = _material_changed(existing, observation)
    current_material, current_fields = _material_fingerprint(observation)
    resurfaced_version = False

    if surface_repost:
        version_key = f"{matched_key}:repost:{current_material or day}"
        version_match = next(
            (
                key
                for key, entry in seen.items()
                if isinstance(entry, Mapping)
                and entry.get("repost_of") == matched_key
                and entry.get("material_fingerprint") == current_material
            ),
            None,
        )
        if version_match is None:
            version_key = _unique_key(seen, version_key)
            version = _new_entry(observation, identity, day)
            version["repost_of"] = matched_key
            version["status"] = disposition if disposition is not None else "new"
            seen[version_key] = version
            return ObservationResult(
                version_key,
                True,
                True,
                material_changed,
                _string(version["status"]),
            )
        matched_key = str(version_match)
        existing = seen[matched_key]
        material_changed = False
        resurfaced_version = True

    reserved = {
        "first_seen",
        "last_seen",
        "observation_count",
        "identity",
        "material_fingerprint",
        "material_fields",
        "repost_of",
        "status",
    }
    for field, value in observation.items():
        if field not in reserved:
            existing[field] = value

    existing.setdefault("first_seen", day)
    existing["last_seen"] = day
    existing["observation_count"] = int(existing.get("observation_count", 0) or 0) + 1
    _set_identity(existing, identity)
    if current_material:
        existing["material_fingerprint"] = current_material
        existing["material_fields"] = list(current_fields)

    if disposition is not None:
        existing["status"] = disposition
    elif not resurfaced_version and _status(observation.get("status")) not in {"", "new"}:
        existing["status"] = observation["status"]
    existing.setdefault("status", "new")

    return ObservationResult(
        str(matched_key),
        False,
        False,
        material_changed,
        _string(existing["status"]),
    )


def query_jobs(
    ledger: Mapping[str, Any],
    *,
    statuses: Iterable[str] | None = None,
    new_only: bool = False,
) -> list[tuple[str, Mapping[str, Any]]]:
    """Return queryable entries, optionally narrowed to default-new jobs."""

    seen = ledger.get("seen", {})
    if not isinstance(seen, Mapping):
        return []
    requested = {_status(value) for value in statuses} if statuses is not None else None
    result: list[tuple[str, Mapping[str, Any]]] = []
    for key, entry in seen.items():
        if not isinstance(entry, Mapping):
            continue
        entry_status = _status(entry.get("status", "new"))
        if requested is not None and entry_status not in requested:
            continue
        if new_only and (entry_status != "new" or entry_status in CLOSED_STATUSES):
            continue
        result.append((str(key), entry))
    return result


def load_ledger(path: str | Path) -> dict[str, Any]:
    """Load a private ledger, accepting the legacy empty/missing state."""

    ledger_path = Path(path)
    if not ledger_path.exists():
        return {"seen": {}}
    with ledger_path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("ledger root must be an object")
    seen = value.setdefault("seen", {})
    if not isinstance(seen, dict):
        raise ValueError("ledger['seen'] must be an object")
    return value


def save_ledger(path: str | Path, ledger: Mapping[str, Any]) -> None:
    """Persist the ledger as ordinary JSON without changing its private path."""

    ledger_path = Path(path)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(
        json.dumps(ledger, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


__all__ = [
    "CLOSED_STATUSES",
    "ObservationResult",
    "company_title_location_fingerprint",
    "identity_key",
    "load_ledger",
    "normalize_canonical_url",
    "observe",
    "query_jobs",
    "save_ledger",
]
