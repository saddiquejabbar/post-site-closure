from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


class CatalogError(ValueError):
    pass


def normalize_label(value: str) -> str:
    value = value.casefold().strip()
    value = re.sub(r"[\s_]+", " ", value)
    value = re.sub(r"\s*[-–—]\s*", "-", value)
    value = re.sub(r"[^a-z0-9.+()/\- ]+", "", value)
    return re.sub(r"\s+", " ", value).strip()


@dataclass(slots=True, frozen=True)
class Resolution:
    sku: str | None
    candidates: tuple[str, ...]
    method: str


class SkuCatalog:
    def __init__(self, headers: Iterable[str], aliases: dict[str, str | list[str]] | None = None) -> None:
        exact = [str(item).strip() for item in headers if str(item).strip()]
        if len(exact) != len(set(exact)):
            raise CatalogError("SKU headers must be unique")
        self.headers = tuple(exact)
        self._header_set = set(exact)
        self._normalized: dict[str, list[str]] = {}
        for header in exact:
            self._normalized.setdefault(normalize_label(header), []).append(header)
        self._aliases: dict[str, tuple[str, ...]] = {}
        for raw_alias, raw_targets in (aliases or {}).items():
            alias = normalize_label(raw_alias)
            targets = [raw_targets] if isinstance(raw_targets, str) else list(raw_targets)
            canonical = tuple(dict.fromkeys(str(item).strip() for item in targets))
            if not alias or not canonical:
                raise CatalogError(f"Invalid alias: {raw_alias!r}")
            unknown = [item for item in canonical if item not in self._header_set]
            if unknown:
                raise CatalogError(f"Alias {raw_alias!r} targets unknown SKU headers: {unknown}")
            self._aliases[alias] = canonical

    @classmethod
    def from_json_file(cls, headers: Iterable[str], path: Path) -> "SkuCatalog":
        if not path.exists():
            return cls(headers)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CatalogError(f"Could not read SKU alias file: {path}") from exc
        if not isinstance(raw, dict):
            raise CatalogError("SKU alias file must contain a JSON object")
        return cls(headers, raw)

    def resolve(self, label: str, *, max_candidates: int = 3) -> Resolution:
        stripped = label.strip()
        if stripped in self._header_set:
            return Resolution(stripped, (), "exact_header")
        normalized = normalize_label(stripped)
        matches = self._normalized.get(normalized, [])
        if len(matches) == 1:
            return Resolution(matches[0], (), "normalized_header")
        if len(matches) > 1:
            return Resolution(None, tuple(matches[:max_candidates]), "ambiguous_header")
        alias_targets = self._aliases.get(normalized)
        if alias_targets:
            if len(alias_targets) == 1:
                return Resolution(alias_targets[0], (), "approved_alias")
            return Resolution(None, alias_targets[:max_candidates], "ambiguous_alias")
        close = difflib.get_close_matches(normalized, list(self._normalized) + list(self._aliases), n=8, cutoff=0.62)
        candidates: list[str] = []
        for choice in close:
            targets = self._aliases.get(choice) or tuple(self._normalized.get(choice, []))
            for target in targets:
                if target not in candidates:
                    candidates.append(target)
                if len(candidates) >= max_candidates:
                    break
            if len(candidates) >= max_candidates:
                break
        return Resolution(None, tuple(candidates), "unresolved")
