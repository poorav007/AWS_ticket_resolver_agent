"""Runbook loader and matcher.

The agent should ground its diagnosis in a runbook rather than free-form
reasoning. Runbooks are Markdown files in ``runbooks/`` with a small YAML-ish
front-matter block so they can be matched to a ticket by symptom.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

log = logging.getLogger("ticket-resolver.runbooks")

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _parse_front_matter(text: str) -> tuple[dict[str, Any], str]:
    """Parse a deliberately tiny front-matter subset.

    Supports ``key: value`` and ``key: [a, b, c]``. A real YAML dependency is
    not worth it for a handful of scalar fields.
    """
    match = _FRONTMATTER.match(text)
    if not match:
        return {}, text

    meta: dict[str, Any] = {}
    for line in match.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            meta[key] = [
                v.strip().strip("\"'")
                for v in value[1:-1].split(",")
                if v.strip()
            ]
        else:
            meta[key] = value.strip().strip("\"'")

    return meta, text[match.end():]


class Runbook:
    """One runbook document plus its matching metadata."""

    def __init__(self, slug: str, meta: dict[str, Any], body: str) -> None:
        self.slug = slug
        self.meta = meta
        self.body = body.strip()

    @property
    def title(self) -> str:
        return str(self.meta.get("title") or self.slug)

    @property
    def symptoms(self) -> list[str]:
        value = self.meta.get("symptoms", [])
        return [str(v) for v in value] if isinstance(value, list) else [str(value)]

    @property
    def services(self) -> list[str]:
        value = self.meta.get("services", [])
        return [str(v) for v in value] if isinstance(value, list) else [str(value)]

    def score(self, *, text: str = "", symptoms: list[str] | None = None) -> int:
        """Rank how well this runbook matches a ticket.

        Symptom overlap dominates; free-text overlap breaks ties.
        """
        haystack = " ".join([text, *(symptoms or [])]).lower()
        score = 0
        for symptom in self.symptoms:
            needle = symptom.lower().strip()
            if needle and needle in haystack:
                score += 10
        for service in self.services:
            if service.lower() in haystack:
                score += 3
        # Whole-word hits on individual words give a weak signal.
        for word in re.findall(r"[a-z0-9_]{5,}", haystack):
            if word in self.body.lower():
                score += 1
        return score

    def to_dict(self, include_body: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "slug": self.slug,
            "title": self.title,
            "symptoms": self.symptoms,
            "services": self.services,
        }
        if include_body:
            payload["content"] = self.body
        return payload


class RunbookLibrary:
    """All runbooks on disk, with symptom-based lookup."""

    def __init__(self, runbooks_dir: Path) -> None:
        self._dir = Path(runbooks_dir)
        self._cache: dict[str, Runbook] | None = None

    def _load(self) -> dict[str, Runbook]:
        if self._cache is not None:
            return self._cache

        books: dict[str, Runbook] = {}
        if self._dir.exists():
            for path in sorted(self._dir.glob("*.md")):
                try:
                    meta, body = _parse_front_matter(path.read_text())
                except OSError as exc:
                    log.warning("Skipping unreadable runbook %s: %s", path, exc)
                    continue
                books[path.stem] = Runbook(path.stem, meta, body)
        self._cache = books
        return books

    def list_slugs(self) -> list[str]:
        return sorted(self._load())

    def get(self, slug: str) -> Runbook:
        books = self._load()
        if slug in books:
            return books[slug]
        # Tolerate a title or a filename with/without the .md suffix.
        for key, book in books.items():
            if slug.lower() in {key.lower(), book.title.lower()}:
                return book
        raise KeyError(
            f"Runbook {slug!r} not found. Available: {', '.join(self.list_slugs()) or 'none'}"
        )

    def search(
        self, *, text: str = "", symptoms: list[str] | None = None, limit: int = 3
    ) -> list[Runbook]:
        """Return the best-matching runbooks, highest score first."""
        ranked = [
            (book.score(text=text, symptoms=symptoms), book)
            for book in self._load().values()
        ]
        ranked = [pair for pair in ranked if pair[0] > 0]
        ranked.sort(key=lambda pair: pair[0], reverse=True)
        return [book for _, book in ranked[:limit]]
