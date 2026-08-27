"""Deterministic navigation over the capstone's curated LLM Wiki.

The wiki is intentionally a small, linked knowledge graph rather than a bag of
anonymous text chunks. Each page has a stable identity, declared relationships,
and authoritative sources. Retrieval returns a trace that can be cited in a
MigrationManifest and reproduced without an embedding service.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

from pydantic import AnyHttpUrl, Field, field_validator, model_validator

from legacy_migration_agent.contracts import (
    SCHEMA_VERSION,
    Platform,
    StrictModel,
    validate_relative_path,
)
from legacy_migration_agent.core.integrity import artifact_digest
from legacy_migration_agent.core.policies import PolicyViolation

TOKEN_PATTERN = re.compile(r"[a-z0-9][a-z0-9_+-]*")
EXACT_DIAGNOSTIC_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]*[_.][a-z0-9_.-]*$")
TOP_LEVEL_LIST_ITEM_PATTERN = re.compile(r"^(?:[-+*]|\d+[.)])\s+")
MAX_RETRIEVAL_PAGES = 3
MAX_SELECTED_CONTENT_CHARS = 1600
INDEX_FILENAME = "index.md"
_ORACLE_SEGMENTS = frozenset({"expected", "golden", "oracle"})


class WikiSource(StrictModel):
    title: str = Field(min_length=1, max_length=300)
    url: AnyHttpUrl
    publisher: str = Field(min_length=1, max_length=160)
    authority: Literal["official", "standard", "project", "research"]


class WikiPageRecord(StrictModel):
    page_id: str = Field(min_length=1, max_length=160, pattern=r"^[a-z0-9][a-z0-9-]*$")
    title: str = Field(min_length=1, max_length=300)
    path: str
    summary: str = Field(min_length=1, max_length=2000)
    platforms: tuple[Platform, ...] = ()
    source_version: str = Field(min_length=1, max_length=160)
    target_version: str = Field(min_length=1, max_length=160)
    status: Literal["pilot", "reviewed", "deprecated"]
    owner: str = Field(min_length=1, max_length=160)
    last_verified: date
    tags: tuple[str, ...] = Field(min_length=1)
    links: tuple[str, ...] = ()
    sources: tuple[WikiSource, ...] = Field(min_length=1)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        normalized = validate_relative_path(value)
        if not normalized.endswith(".md"):
            raise ValueError("wiki page paths must reference Markdown files")
        return normalized

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(dict.fromkeys(value.strip().lower() for value in values))
        if any(not value for value in normalized):
            raise ValueError("wiki tags cannot be blank")
        return normalized


class WikiCatalog(StrictModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    pages: tuple[WikiPageRecord, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_graph(self) -> WikiCatalog:
        page_ids = [page.page_id for page in self.pages]
        if len(page_ids) != len(set(page_ids)):
            raise ValueError("wiki page IDs must be unique")
        page_paths = [page.path for page in self.pages]
        if len(page_paths) != len(set(page_paths)):
            raise ValueError("wiki page paths must be unique")
        known = set(page_ids)
        for page in self.pages:
            missing = sorted(set(page.links) - known)
            if missing:
                raise ValueError(
                    f"wiki page {page.page_id} links to unknown pages: {', '.join(missing)}"
                )
            if page.page_id in page.links:
                raise ValueError(f"wiki page {page.page_id} cannot link to itself")
        return self


class RetrievalHit(StrictModel):
    page_id: str
    title: str
    path: str
    score: float = Field(ge=0)
    matched_fields: tuple[str, ...]
    expanded_from: str | None = None
    source_version: str
    target_version: str
    status: Literal["pilot", "reviewed", "deprecated"]
    owner: str
    last_verified: date
    page_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    selected_content: str = Field(min_length=1, max_length=MAX_SELECTED_CONTENT_CHARS)
    selected_content_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    content_kind: Literal["curated_wiki_evidence"] = "curated_wiki_evidence"
    sources: tuple[WikiSource, ...]

    @model_validator(mode="after")
    def validate_selected_content_digest(self) -> RetrievalHit:
        expected = _text_digest(self.selected_content)
        if self.selected_content_digest != expected:
            raise ValueError("selected Wiki content does not match its digest")
        return self


class RetrievalTrace(StrictModel):
    retrieval_strategy: Literal["deterministic_lexical"] = "deterministic_lexical"
    query: str = Field(min_length=1)
    normalized_terms: tuple[str, ...]
    platform: Platform | None = None
    source_version: str | None = None
    target_version: str | None = None
    catalog_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    as_of: date
    max_age_days: int = Field(ge=1)
    max_primary_hits: int = Field(ge=1, le=MAX_RETRIEVAL_PAGES)
    expand_links: bool
    hits: tuple[RetrievalHit, ...]
    evidence_bundle_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_evidence_bundle(self) -> RetrievalTrace:
        if len(self.hits) > MAX_RETRIEVAL_PAGES:
            raise ValueError(f"Wiki retrieval is limited to {MAX_RETRIEVAL_PAGES} selected pages")
        page_ids = tuple(hit.page_id for hit in self.hits)
        if len(page_ids) != len(set(page_ids)):
            raise ValueError("Wiki retrieval cannot select the same page more than once")
        expected = _evidence_bundle_digest(self.catalog_digest, self.hits)
        if self.evidence_bundle_digest != expected:
            raise ValueError("Wiki evidence bundle does not match its digest")
        return self


@dataclass(frozen=True)
class _LoadedPage:
    record: WikiPageRecord
    body: str


@dataclass(frozen=True)
class _WikiTreeEntry:
    path: str
    kind: Literal["directory", "file"]
    device: int
    inode: int
    mode: int
    size: int
    modified_ns: int
    changed_ns: int


class LlmWiki:
    """Load and navigate a validated file-backed knowledge graph."""

    def __init__(
        self,
        root: Path,
        catalog: WikiCatalog,
        pages: dict[str, _LoadedPage],
        *,
        index_body: str,
    ):
        self.root = root
        self.catalog = catalog
        self._pages = pages
        self.catalog_digest = artifact_digest(catalog)
        self.index_digest = _text_digest(index_body)
        self._page_digests = {
            page_id: f"sha256:{hashlib.sha256(page.body.encode('utf-8')).hexdigest()}"
            for page_id, page in pages.items()
        }

    @classmethod
    def load(cls, root: Path) -> LlmWiki:
        resolved_root, root_descriptor, inventory = _preflight_wiki_tree(root)
        try:
            try:
                catalog_body = _read_wiki_snapshot_text(
                    root_descriptor,
                    inventory,
                    "catalog.json",
                )
            except FileNotFoundError as exc:
                raise PolicyViolation("wiki catalog.json is missing") from exc
            try:
                catalog = WikiCatalog.model_validate_json(catalog_body)
            except ValueError as exc:
                raise PolicyViolation("wiki catalog.json is malformed or invalid") from exc
            declared_paths = {record.path for record in catalog.pages}
            actual_paths = {
                entry.path
                for entry in inventory
                if entry.kind == "file"
                and entry.path.startswith("pages/")
                and entry.path.endswith(".md")
            }
            missing_paths = sorted(declared_paths - actual_paths)
            extra_paths = sorted(actual_paths - declared_paths)
            if missing_paths or extra_paths:
                details: list[str] = []
                if missing_paths:
                    details.append(f"missing: {', '.join(missing_paths)}")
                if extra_paths:
                    details.append(f"unindexed: {', '.join(extra_paths)}")
                raise PolicyViolation(
                    "wiki page inventory does not match catalog.json (" + "; ".join(details) + ")"
                )

            pages: dict[str, _LoadedPage] = {}
            for record in catalog.pages:
                pages[record.page_id] = _LoadedPage(
                    record=record,
                    body=_read_wiki_snapshot_text(
                        root_descriptor,
                        inventory,
                        record.path,
                    ),
                )

            try:
                index_body = _read_wiki_snapshot_text(
                    root_descriptor,
                    inventory,
                    INDEX_FILENAME,
                )
            except FileNotFoundError as exc:
                raise PolicyViolation(f"wiki {INDEX_FILENAME} is missing") from exc
            expected_index = render_wiki_index(catalog, pages)
            if index_body != expected_index:
                raise PolicyViolation(
                    f"wiki {INDEX_FILENAME} is stale or tampered; regenerate it from catalog.json"
                )
            _verify_wiki_root_path_identity(resolved_root, root_descriptor)
            if _scan_wiki_tree(root_descriptor) != inventory:
                raise PolicyViolation("wiki tree changed while its evidence snapshot was loaded")
        finally:
            os.close(root_descriptor)
        return cls(
            resolved_root,
            catalog,
            pages,
            index_body=index_body,
        )

    def read_page(self, page_id: str) -> str:
        try:
            return self._pages[page_id].body
        except KeyError as exc:
            raise KeyError(f"unknown wiki page: {page_id}") from exc

    def search(
        self,
        query: str,
        *,
        platform: Platform | None = None,
        source_version: str | None = None,
        target_version: str | None = None,
        max_primary_hits: int = 3,
        expand_links: bool = True,
        as_of: date | None = None,
        max_age_days: int = 365,
        required_exact_ids: Iterable[str] = (),
    ) -> RetrievalTrace:
        terms = _tokens(query)
        if not terms:
            raise ValueError("query must contain at least one searchable term")
        if not 1 <= max_primary_hits <= MAX_RETRIEVAL_PAGES:
            raise ValueError(f"max_primary_hits must be between 1 and {MAX_RETRIEVAL_PAGES}")
        if max_age_days < 1:
            raise ValueError("max_age_days must be positive")
        if platform is not None and (source_version is None or target_version is None):
            raise ValueError(
                "platform-specific Wiki retrieval requires source_version and target_version"
            )
        effective_as_of = as_of or date.today()
        oldest_allowed = effective_as_of - timedelta(days=max_age_days)

        exact_ids = _normalize_required_exact_ids(required_exact_ids, query=query)
        eligible_pages = {
            page_id: loaded
            for page_id, loaded in self._pages.items()
            if _page_is_eligible(
                loaded.record,
                platform=platform,
                source_version=source_version,
                target_version=target_version,
                oldest_allowed=oldest_allowed,
                effective_as_of=effective_as_of,
            )
        }

        if exact_ids:
            exact_hits = self._exact_id_hits(
                exact_ids,
                eligible_pages=eligible_pages,
                terms=terms,
            )
            return RetrievalTrace(
                retrieval_strategy="deterministic_lexical",
                query=query,
                normalized_terms=tuple(sorted(terms)),
                platform=platform,
                source_version=source_version,
                target_version=target_version,
                catalog_digest=self.catalog_digest,
                as_of=effective_as_of,
                max_age_days=max_age_days,
                max_primary_hits=max_primary_hits,
                expand_links=False,
                hits=exact_hits,
                evidence_bundle_digest=_evidence_bundle_digest(
                    self.catalog_digest,
                    exact_hits,
                ),
            )

        ranked: list[tuple[float, str, tuple[str, ...]]] = []
        for page_id, loaded in eligible_pages.items():
            score, fields = _score_page(terms, loaded)
            if score > 0:
                ranked.append((score, page_id, fields))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        primary = ranked[:max_primary_hits]

        hits: list[RetrievalHit] = [
            self._hit(page_id, score, fields, terms=terms) for score, page_id, fields in primary
        ]
        seen = {hit.page_id for hit in hits}
        if expand_links:
            for _, primary_id, _ in primary:
                for linked_id in self._pages[primary_id].record.links:
                    if len(hits) >= MAX_RETRIEVAL_PAGES:
                        break
                    if linked_id in seen:
                        continue
                    linked = eligible_pages.get(linked_id)
                    if linked is None:
                        continue
                    hits.append(
                        self._hit(
                            linked_id,
                            0.25,
                            ("linked-page",),
                            terms=terms,
                            expanded_from=primary_id,
                        )
                    )
                    seen.add(linked_id)
                if len(hits) >= MAX_RETRIEVAL_PAGES:
                    break

        evidence_bundle_digest = _evidence_bundle_digest(self.catalog_digest, hits)

        return RetrievalTrace(
            retrieval_strategy="deterministic_lexical",
            query=query,
            normalized_terms=tuple(sorted(terms)),
            platform=platform,
            source_version=source_version,
            target_version=target_version,
            catalog_digest=self.catalog_digest,
            as_of=effective_as_of,
            max_age_days=max_age_days,
            max_primary_hits=max_primary_hits,
            expand_links=expand_links,
            hits=tuple(hits),
            evidence_bundle_digest=evidence_bundle_digest,
        )

    def _hit(
        self,
        page_id: str,
        score: float,
        matched_fields: Iterable[str],
        terms: set[str],
        expanded_from: str | None = None,
        required_exact_ids: tuple[str, ...] = (),
    ) -> RetrievalHit:
        record = self._pages[page_id].record
        selected_content = _select_content(
            self._pages[page_id].body,
            terms=terms,
            required_exact_ids=required_exact_ids,
        )
        return RetrievalHit(
            page_id=page_id,
            title=record.title,
            path=record.path,
            score=score,
            matched_fields=tuple(matched_fields),
            expanded_from=expanded_from,
            source_version=record.source_version,
            target_version=record.target_version,
            status=record.status,
            owner=record.owner,
            last_verified=record.last_verified,
            page_digest=self._page_digests[page_id],
            selected_content=selected_content,
            selected_content_digest=_text_digest(selected_content),
            sources=record.sources,
        )

    def _exact_id_hits(
        self,
        exact_ids: tuple[str, ...],
        *,
        eligible_pages: dict[str, _LoadedPage],
        terms: set[str],
    ) -> tuple[RetrievalHit, ...]:
        """Select the one eligible curated page that owns each exact diagnostic ID."""

        page_ids_by_signal: dict[str, str] = {}
        for exact_id in exact_ids:
            matches = tuple(
                sorted(
                    page_id
                    for page_id, loaded in eligible_pages.items()
                    if _page_contains_exact_id(loaded, exact_id)
                )
            )
            if not matches:
                raise PolicyViolation(
                    "required exact Wiki diagnostic has no eligible curated page: " + exact_id
                )
            if len(matches) != 1:
                raise PolicyViolation(
                    "required exact Wiki diagnostic is ambiguous across curated pages: " + exact_id
                )
            page_ids_by_signal[exact_id] = matches[0]

        exact_ids_by_page: dict[str, list[str]] = {}
        for exact_id in exact_ids:
            exact_ids_by_page.setdefault(page_ids_by_signal[exact_id], []).append(exact_id)
        if len(exact_ids_by_page) > MAX_RETRIEVAL_PAGES:
            raise PolicyViolation(
                "required exact Wiki diagnostics exceed the bounded selected-page limit"
            )

        ranked_pages: list[tuple[float, str, tuple[str, ...]]] = []
        for page_id in exact_ids_by_page:
            score, matched_fields = _score_page(terms, eligible_pages[page_id])
            ranked_pages.append(
                (
                    score,
                    page_id,
                    tuple(dict.fromkeys((*matched_fields, "exact-diagnostic-id"))),
                )
            )
        ranked_pages.sort(key=lambda item: (-item[0], item[1]))

        hits = tuple(
            self._hit(
                page_id,
                score,
                fields,
                terms=terms,
                required_exact_ids=tuple(exact_ids_by_page[page_id]),
            )
            for score, page_id, fields in ranked_pages
        )
        selected = "\n".join(hit.selected_content for hit in hits)
        missing = tuple(
            exact_id
            for exact_id in exact_ids
            if not contains_exact_diagnostic_id(selected, exact_id)
        )
        if missing:  # pragma: no cover - _select_content owns the concrete invariant
            raise PolicyViolation(
                "selected Wiki excerpts omit required exact diagnostics: " + ", ".join(missing)
            )
        return hits


def _preflight_wiki_tree(root: Path) -> tuple[Path, int, tuple[_WikiTreeEntry, ...]]:
    """Open and inventory the Wiki without following any pathname links."""

    candidate = Path(root)
    try:
        metadata = candidate.lstat()
    except FileNotFoundError as exc:
        raise PolicyViolation("wiki root does not exist") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise PolicyViolation("wiki root cannot be a symlink")
    if not stat.S_ISDIR(metadata.st_mode):
        raise PolicyViolation("wiki root must be a directory")
    resolved = candidate.resolve(strict=True)
    if any(part.casefold() in _ORACLE_SEGMENTS for part in resolved.parts):
        raise PolicyViolation("wiki root cannot contain expected, golden, or oracle segments")
    try:
        descriptor = os.open(resolved, _wiki_directory_flags())
    except OSError as exc:
        raise PolicyViolation("wiki root could not be opened without following links") from exc
    opened = os.fstat(descriptor)
    if _wiki_stat_fingerprint(opened) != _wiki_stat_fingerprint(metadata):
        os.close(descriptor)
        raise PolicyViolation("wiki root identity changed while being opened")
    try:
        inventory = _scan_wiki_tree(descriptor)
    except BaseException:
        os.close(descriptor)
        raise
    return resolved, descriptor, inventory


def _scan_wiki_tree(root_descriptor: int) -> tuple[_WikiTreeEntry, ...]:
    entries: list[_WikiTreeEntry] = []

    def scan(directory_descriptor: int, prefix: tuple[str, ...]) -> None:
        try:
            with os.scandir(directory_descriptor) as children:
                ordered = tuple(sorted(children, key=lambda child: child.name))
        except OSError as exc:
            raise PolicyViolation("wiki directory inventory could not be inspected safely") from exc
        for child in ordered:
            if child.name.casefold() in _ORACLE_SEGMENTS:
                raise PolicyViolation(
                    "wiki tree cannot contain expected, golden, or oracle segments"
                )
            relative_parts = (*prefix, child.name)
            relative = "/".join(relative_parts)
            try:
                metadata = child.stat(follow_symlinks=False)
            except OSError as exc:
                raise PolicyViolation(
                    f"wiki path could not be inspected safely: {relative}"
                ) from exc
            if stat.S_ISLNK(metadata.st_mode):
                raise PolicyViolation(f"wiki tree cannot contain symlinks: {relative}")
            if stat.S_ISDIR(metadata.st_mode):
                entries.append(_wiki_tree_entry(relative, "directory", metadata))
                try:
                    child_descriptor = os.open(
                        child.name,
                        _wiki_directory_flags(),
                        dir_fd=directory_descriptor,
                    )
                except OSError as exc:
                    raise PolicyViolation(
                        f"wiki directory changed or became unsafe: {relative}"
                    ) from exc
                try:
                    opened = os.fstat(child_descriptor)
                    if _wiki_stat_fingerprint(opened) != _wiki_stat_fingerprint(metadata):
                        raise PolicyViolation(
                            f"wiki directory identity changed while being opened: {relative}"
                        )
                    scan(child_descriptor, relative_parts)
                finally:
                    os.close(child_descriptor)
                continue
            if stat.S_ISREG(metadata.st_mode):
                entries.append(_wiki_tree_entry(relative, "file", metadata))
                continue
            raise PolicyViolation(f"wiki tree contains an unsupported special file: {relative}")

    try:
        scan_descriptor = os.open(".", _wiki_directory_flags(), dir_fd=root_descriptor)
    except OSError as exc:
        raise PolicyViolation("wiki root identity changed while inventory was captured") from exc
    try:
        if _wiki_stat_fingerprint(os.fstat(scan_descriptor)) != _wiki_stat_fingerprint(
            os.fstat(root_descriptor)
        ):
            raise PolicyViolation("wiki root identity changed while inventory was captured")
        scan(scan_descriptor, ())
    finally:
        os.close(scan_descriptor)
    return tuple(sorted(entries, key=lambda entry: entry.path))


def _read_wiki_snapshot_text(
    root_descriptor: int,
    inventory: tuple[_WikiTreeEntry, ...],
    relative_path: str,
) -> str:
    """Read one preflighted file by descriptor and reject identity changes."""

    normalized = validate_relative_path(relative_path)
    by_path = {entry.path: entry for entry in inventory}
    expected = by_path.get(normalized)
    if expected is None:
        raise FileNotFoundError(normalized)
    if expected.kind != "file":
        raise PolicyViolation(f"wiki evidence path is not a regular file: {normalized}")

    parts = normalized.split("/")
    opened_directories: list[int] = []
    directory_descriptor = root_descriptor
    chunks: list[bytes] = []
    try:
        prefix: list[str] = []
        for part in parts[:-1]:
            prefix.append(part)
            directory_path = "/".join(prefix)
            expected_directory = by_path.get(directory_path)
            if expected_directory is None or expected_directory.kind != "directory":
                raise PolicyViolation(
                    f"wiki evidence parent is not in the preflight snapshot: {directory_path}"
                )
            try:
                child_descriptor = os.open(
                    part,
                    _wiki_directory_flags(),
                    dir_fd=directory_descriptor,
                )
            except OSError as exc:
                raise PolicyViolation(
                    f"wiki evidence parent changed or became unsafe: {directory_path}"
                ) from exc
            opened_directories.append(child_descriptor)
            directory_descriptor = child_descriptor
            if (
                _wiki_tree_entry(
                    directory_path,
                    "directory",
                    os.fstat(directory_descriptor),
                )
                != expected_directory
            ):
                raise PolicyViolation(f"wiki evidence parent identity changed: {directory_path}")

        try:
            descriptor = os.open(
                parts[-1],
                os.O_RDONLY | _wiki_no_follow_flag(),
                dir_fd=directory_descriptor,
            )
        except OSError as exc:
            raise PolicyViolation(
                f"wiki evidence file changed or became unsafe: {normalized}"
            ) from exc
        try:
            before = os.fstat(descriptor)
            if _wiki_tree_entry(normalized, "file", before) != expected:
                raise PolicyViolation(f"wiki evidence file identity changed: {normalized}")
            while chunk := os.read(descriptor, 64 * 1024):
                chunks.append(chunk)
            after = os.fstat(descriptor)
            if _wiki_tree_entry(normalized, "file", after) != expected:
                raise PolicyViolation(f"wiki evidence file changed while read: {normalized}")
        finally:
            os.close(descriptor)
    finally:
        for descriptor in reversed(opened_directories):
            os.close(descriptor)
    try:
        return b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PolicyViolation(f"wiki evidence file is not valid UTF-8: {normalized}") from exc


def _wiki_tree_entry(
    path: str,
    kind: Literal["directory", "file"],
    metadata: os.stat_result,
) -> _WikiTreeEntry:
    return _WikiTreeEntry(
        path=path,
        kind=kind,
        device=metadata.st_dev,
        inode=metadata.st_ino,
        mode=metadata.st_mode,
        size=metadata.st_size,
        modified_ns=metadata.st_mtime_ns,
        changed_ns=metadata.st_ctime_ns,
    )


def _wiki_stat_fingerprint(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _verify_wiki_root_path_identity(root: Path, descriptor: int) -> None:
    try:
        current = root.lstat()
    except FileNotFoundError as exc:
        raise PolicyViolation(
            "wiki root path changed while its evidence snapshot was loaded"
        ) from exc
    if stat.S_ISLNK(current.st_mode) or not stat.S_ISDIR(current.st_mode):
        raise PolicyViolation("wiki root path changed while its evidence snapshot was loaded")
    if _wiki_stat_fingerprint(current) != _wiki_stat_fingerprint(os.fstat(descriptor)):
        raise PolicyViolation("wiki root identity changed while its evidence snapshot was loaded")


def _wiki_directory_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _wiki_no_follow_flag()


def _wiki_no_follow_flag() -> int:
    try:
        return os.O_NOFOLLOW
    except AttributeError as exc:  # pragma: no cover - supported target is POSIX
        raise RuntimeError("secure Wiki loading requires O_NOFOLLOW") from exc


def render_wiki_index(
    catalog: WikiCatalog,
    pages: dict[str, _LoadedPage],
) -> str:
    """Render the canonical human-readable catalog and page inventory.

    The checked-in Markdown is intentionally reproducible. Loading the Wiki
    compares it byte-for-byte with this representation so a missing page,
    catalog edit, page-content edit, or hand-edited inventory fails visibly.
    """

    lines = [
        "# Curated LLM Wiki index",
        "",
        "This is the human-readable inventory for the version-controlled Wiki used by",
        "the migration Architect. Retrieval is deterministic lexical navigation over",
        "one to three curated pages; this index is not a vector database or an",
        "instruction source.",
        "",
        f"Catalog digest: `{artifact_digest(catalog)}`",
        "",
        "| Page ID | Title | Path | Platform | Version | Status | Last verified | Content digest |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for record in catalog.pages:
        loaded = pages.get(record.page_id)
        if loaded is None:
            raise ValueError(f"cannot render index without page: {record.page_id}")
        platforms = ", ".join(platform.value for platform in record.platforms) or "workflow"
        version = (
            record.source_version
            if record.source_version == record.target_version
            else f"{record.source_version} to {record.target_version}"
        )
        page_digest = _text_digest(loaded.body)
        lines.append(
            "| "
            + " | ".join(
                (
                    _escape_markdown_cell(record.page_id),
                    _escape_markdown_cell(record.title),
                    f"`{_escape_markdown_cell(record.path)}`",
                    _escape_markdown_cell(platforms),
                    _escape_markdown_cell(version),
                    record.status,
                    record.last_verified.isoformat(),
                    f"`{page_digest}`",
                )
            )
            + " |"
        )
    lines.extend(
        (
            "",
            "The catalog owns page identity, versions, links, status, and authoritative",
            "sources. Each content digest binds the corresponding Markdown page. If this",
            "inventory differs from `catalog.json` or the page files, Wiki loading stops",
            "instead of silently retrieving stale guidance.",
            "",
        )
    )
    return "\n".join(lines)


def _tokens(value: str) -> set[str]:
    return set(TOKEN_PATTERN.findall(value.lower()))


def _normalize_required_exact_ids(
    values: Iterable[str],
    *,
    query: str,
) -> tuple[str, ...]:
    normalized = tuple(sorted(dict.fromkeys(value.strip() for value in values)))
    if any(EXACT_DIAGNOSTIC_ID_PATTERN.fullmatch(value) is None for value in normalized):
        raise ValueError("required exact Wiki diagnostic IDs are malformed")
    query_tokens = set(query.split())
    omitted = tuple(value for value in normalized if value not in query_tokens)
    if omitted:
        raise ValueError(
            "required exact Wiki diagnostic IDs must occur verbatim in the query: "
            + ", ".join(omitted)
        )
    return normalized


def contains_exact_diagnostic_id(text: str, exact_id: str) -> bool:
    """Match one diagnostic token without accepting a longer identifier substring."""

    if EXACT_DIAGNOSTIC_ID_PATTERN.fullmatch(exact_id) is None:
        raise ValueError("exact Wiki diagnostic ID is malformed")
    boundary = rf"(?<![A-Za-z0-9_.-]){re.escape(exact_id)}(?![A-Za-z0-9_.-])"
    return re.search(boundary, text) is not None


def _page_is_eligible(
    record: WikiPageRecord,
    *,
    platform: Platform | None,
    source_version: str | None,
    target_version: str | None,
    oldest_allowed: date,
    effective_as_of: date,
) -> bool:
    if (
        record.status == "deprecated"
        or record.last_verified < oldest_allowed
        or record.last_verified > effective_as_of
    ):
        return False
    if platform is not None and record.platforms and platform not in record.platforms:
        return False
    if source_version is not None and record.source_version != source_version:
        return False
    return target_version is None or record.target_version == target_version


def _page_contains_exact_id(loaded: _LoadedPage, exact_id: str) -> bool:
    if exact_id in loaded.record.tags:
        return True
    return contains_exact_diagnostic_id(loaded.body, exact_id)


def _text_digest(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _select_content(
    body: str,
    *,
    terms: set[str] | None = None,
    required_exact_ids: tuple[str, ...] = (),
) -> str:
    """Return a bounded, deterministic excerpt from curated page content.

    HTML comments and non-printing controls are not exposed to the Architect.
    The result contains source evidence only; it does not contain model scratch
    work or inferred reasoning.
    """

    without_comments = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)
    printable = "".join(
        character
        for character in without_comments.replace("\r\n", "\n").replace("\r", "\n")
        if character in {"\n", "\t"} or ord(character) >= 32
    )
    normalized = "\n".join(line.rstrip() for line in printable.splitlines()).strip()
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    if not normalized:
        raise PolicyViolation("wiki page has no safe content to retrieve")
    if required_exact_ids:
        return _select_exact_content(
            normalized,
            terms=set() if terms is None else terms,
            required_exact_ids=required_exact_ids,
        )
    if len(normalized) <= MAX_SELECTED_CONTENT_CHARS:
        return normalized

    query_terms = set() if terms is None else terms
    if query_terms:
        paragraphs = normalized.split("\n\n")
        ranked: list[tuple[int, int, int]] = []
        for index, paragraph in enumerate(paragraphs):
            matched = query_terms & _tokens(paragraph)
            if not matched:
                continue
            weighted_score = sum(8 if "_" in term else 1 for term in matched)
            ranked.append((weighted_score, len(matched), index))
        ranked.sort(key=lambda item: (-item[0], -item[1], item[2]))
        if ranked:
            selected_indexes = {0}
            for _, _, index in ranked:
                contextual_indexes = {index}
                if index + 1 < len(paragraphs):
                    contextual_indexes.add(index + 1)
                candidate_indexes = sorted(selected_indexes | contextual_indexes)
                candidate = "\n\n".join(paragraphs[item] for item in candidate_indexes)
                if len(candidate) < MAX_SELECTED_CONTENT_CHARS:
                    selected_indexes.update(contextual_indexes)
            selected = "\n\n".join(paragraphs[index] for index in sorted(selected_indexes))
            if len(selected) < MAX_SELECTED_CONTENT_CHARS:
                return selected.rstrip() + "…"

    limit = MAX_SELECTED_CONTENT_CHARS - 1
    selected = normalized[:limit]
    paragraph_boundary = selected.rfind("\n\n")
    if paragraph_boundary >= limit // 2:
        selected = selected[:paragraph_boundary]
    else:
        word_boundary = selected.rfind(" ")
        if word_boundary >= limit // 2:
            selected = selected[:word_boundary]
    return selected.rstrip() + "…"


def _select_exact_content(
    normalized: str,
    *,
    terms: set[str],
    required_exact_ids: tuple[str, ...],
) -> str:
    """Return a bounded excerpt that retains every required diagnostic verbatim."""

    paragraphs = normalized.split("\n\n")
    required_indexes = _required_exact_content_indexes(paragraphs, required_exact_ids)
    selected_indexes = {0, *required_indexes}
    selected = _join_paragraphs(paragraphs, selected_indexes)
    if len(selected) > MAX_SELECTED_CONTENT_CHARS:
        # A Markdown list is one paragraph when its items have no blank lines.
        # Retry with whole top-level list items as semantic units so one exact
        # diagnostic does not pull unrelated sibling bullets into the excerpt.
        # Required units remain intact; an individually oversized unit still
        # fails closed below instead of truncating a diagnostic identifier.
        paragraphs = _split_top_level_list_items(paragraphs)
        required_indexes = _required_exact_content_indexes(
            paragraphs,
            required_exact_ids,
        )
        selected_indexes = {0, *required_indexes}
        selected = _join_paragraphs(paragraphs, selected_indexes)
    if len(selected) > MAX_SELECTED_CONTENT_CHARS:
        raise PolicyViolation(
            "required exact Wiki diagnostic excerpts exceed the bounded content limit"
        )

    contextual_indexes = tuple(
        index + 1 for index in sorted(required_indexes) if index + 1 < len(paragraphs)
    )
    for index in contextual_indexes:
        candidate = _join_paragraphs(paragraphs, {*selected_indexes, index})
        if len(candidate) < MAX_SELECTED_CONTENT_CHARS:
            selected_indexes.add(index)

    ranked: list[tuple[int, int, int]] = []
    for index, paragraph in enumerate(paragraphs):
        if index in selected_indexes:
            continue
        matched = terms & _tokens(paragraph)
        if matched:
            weighted_score = sum(8 if "_" in term else 1 for term in matched)
            ranked.append((weighted_score, len(matched), index))
    ranked.sort(key=lambda item: (-item[0], -item[1], item[2]))
    for _, _, index in ranked:
        candidate = _join_paragraphs(paragraphs, {*selected_indexes, index})
        if len(candidate) < MAX_SELECTED_CONTENT_CHARS:
            selected_indexes.add(index)

    selected = _join_paragraphs(paragraphs, selected_indexes)
    missing = tuple(
        exact_id
        for exact_id in required_exact_ids
        if not contains_exact_diagnostic_id(selected, exact_id)
    )
    if missing:  # pragma: no cover - selected_indexes is derived from exact matches
        raise PolicyViolation(
            "selected Wiki excerpt omits required exact diagnostics: " + ", ".join(missing)
        )
    suffix = "…" if len(selected) < MAX_SELECTED_CONTENT_CHARS else ""
    return selected.rstrip() + suffix


def _required_exact_content_indexes(
    content_units: list[str],
    required_exact_ids: tuple[str, ...],
) -> set[int]:
    required_indexes: set[int] = set()
    for exact_id in required_exact_ids:
        matching_indexes = tuple(
            index
            for index, content in enumerate(content_units)
            if contains_exact_diagnostic_id(content, exact_id)
        )
        if not matching_indexes:
            raise PolicyViolation(
                "eligible Wiki page body omits required exact diagnostic: " + exact_id
            )
        required_indexes.add(
            min(matching_indexes, key=lambda index: (len(content_units[index]), index))
        )
    return required_indexes


def _split_top_level_list_items(paragraphs: list[str]) -> list[str]:
    content_units: list[str] = []
    for paragraph in paragraphs:
        lines = paragraph.splitlines()
        item_starts = tuple(
            index for index, line in enumerate(lines) if TOP_LEVEL_LIST_ITEM_PATTERN.match(line)
        )
        if not item_starts or item_starts[0] != 0:
            content_units.append(paragraph)
            continue
        for position, start in enumerate(item_starts):
            end = item_starts[position + 1] if position + 1 < len(item_starts) else len(lines)
            content_units.append("\n".join(lines[start:end]))
    return content_units


def _join_paragraphs(paragraphs: list[str], indexes: set[int]) -> str:
    return "\n\n".join(paragraphs[index] for index in sorted(indexes))


def _evidence_bundle_digest(
    catalog_digest: str,
    hits: Iterable[RetrievalHit],
) -> str:
    selected_pages = [
        {
            "page_id": hit.page_id,
            "page_digest": hit.page_digest,
            "selected_content": hit.selected_content,
            "selected_content_digest": hit.selected_content_digest,
        }
        for hit in hits
    ]
    return artifact_digest(
        {
            "catalog_digest": catalog_digest,
            "selected_pages": selected_pages,
        }
    )


def _escape_markdown_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _score_page(terms: set[str], loaded: _LoadedPage) -> tuple[float, tuple[str, ...]]:
    record = loaded.record
    fields = {
        "title": (_tokens(record.title), 4.0),
        "tags": (set().union(*(_tokens(tag) for tag in record.tags)), 3.0),
        "summary": (_tokens(record.summary), 2.0),
        "body": (_tokens(loaded.body), 1.0),
    }
    score = 0.0
    matched: list[str] = []
    for field, (tokens, weight) in fields.items():
        overlap = terms & tokens
        if overlap:
            score += len(overlap) * weight
            matched.append(field)
    return score, tuple(matched)
