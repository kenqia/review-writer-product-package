"""Project-scoped product foundation for safe version/history interactions.

The service owns one durable authority per explicit project root: an immutable
version-node directory, branch-head pointers, and one current pointer.  It
does not create a second scientific state model; snapshots remain opaque
metadata owned by the existing manuscript/release layers.
"""

from __future__ import annotations

import copy
import datetime as _datetime
import json
import os
import tempfile
import threading
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from review_writer.project.source_truth import canonical_digest

from .contracts import (
    BranchConflictError,
    BranchPreview,
    CURRENT_POINTER_SCHEMA,
    ConfirmationRequiredError,
    ContextState,
    DownloadArtifact,
    InvalidContextError,
    NonHeadWriteError,
    ProductFoundationError,
    PersistenceError,
    ReadOnlyVersionError,
    SnapshotInvalidError,
    StaleRevisionError,
    UndoPreview,
    UndoTargetError,
    UndoUnavailableError,
    VersionComparison,
    VersionNode,
    VersionNotFoundError,
    VersionView,
    VERSION_CONTEXT_SCHEMA,
    copy_snapshot,
    copy_value,
    validate_identifier,
)
from .project_root import resolve_project_root, version_context_root


class VersionContext:
    """Coordinate one project-instance current and its immutable history tree."""

    def __init__(
        self,
        *,
        project_id: str,
        nodes: dict[str, VersionNode],
        branch_heads: dict[str, str],
        current_version_id: str,
        active_branch_id: str,
        inspected_version_id: str | None,
        revision: int,
        state_path: Path | None = None,
        project_root: Path | None = None,
    ) -> None:
        self._project_id = validate_identifier(project_id, field="project_id")
        self._nodes = nodes
        self._branch_heads = branch_heads
        self._current_version_id = current_version_id
        self._active_branch_id = active_branch_id
        self._inspected_version_id = inspected_version_id
        self._revision = revision
        self._project_root = (
            resolve_project_root(project_root) if project_root is not None else None
        )
        self._context_root = (
            version_context_root(self._project_root)
            if self._project_root is not None
            else None
        )
        self._state_path = (
            self._context_root / "current.json"
            if self._context_root is not None
            else state_path
        )
        self._lock = threading.RLock()
        self._validate_invariants()

    @classmethod
    def create(
        cls,
        snapshot: Mapping[str, Any],
        *,
        project_id: str = "project",
        version_id: str = "v1",
        branch_id: str = "main",
        branch_name: str = "Main",
        state_path: Path | None = None,
        project_root: str | Path | None = None,
    ) -> "VersionContext":
        project_id = validate_identifier(project_id, field="project_id")
        version_id = validate_identifier(version_id, field="version_id")
        branch_id = validate_identifier(branch_id, field="branch_id")
        if not isinstance(branch_name, str) or not branch_name.strip():
            raise InvalidContextError("branch_name is invalid")
        resolved_project_root = cls._resolve_create_project_root(
            project_root,
            state_path,
        )
        node = cls._new_node(
            version_id=version_id,
            parent_version_id=None,
            branch_id=branch_id,
            branch_name=branch_name,
            snapshot=snapshot,
        )
        context = cls(
            project_id=project_id,
            nodes={version_id: node},
            branch_heads={branch_id: version_id},
            current_version_id=version_id,
            active_branch_id=branch_id,
            inspected_version_id=version_id,
            revision=0,
            state_path=None,
            project_root=resolved_project_root,
        )
        context._persist_create()
        return context

    @classmethod
    def load(cls, project_root_or_current_path: str | Path) -> "VersionContext":
        """Load the one project-scoped current and its immutable node files.

        The public adapter normally passes the explicit project directory.  A
        canonical ``.../.review-writer/version_context/current.json`` path is
        also accepted for compatibility with existing callers; arbitrary
        single-file state paths are rejected and never become a second store.
        """

        project_root = cls._coerce_project_root(project_root_or_current_path)
        context_root = version_context_root(project_root)
        versions_root = context_root / "versions"
        branches_root = context_root / "branches"
        current_path = context_root / "current.json"
        try:
            context_entries = tuple(context_root.iterdir())
        except OSError as exc:
            raise PersistenceError("durable version-context directory is unreadable") from exc
        if any(path.is_symlink() for path in context_entries) or {
            path.name for path in context_entries
        } != {"current.json", "versions", "branches"}:
            raise PersistenceError("durable version-context layout is invalid")
        for directory in (context_root, versions_root, branches_root):
            cls._require_directory(directory)
        cls._require_file(current_path)

        current = cls._read_object(current_path)
        if current.get("schema_version") != CURRENT_POINTER_SCHEMA:
            raise InvalidContextError("current pointer schema is invalid")
        if set(current) != {
            "schema_version",
            "project_id",
            "version_id",
            "branch_id",
            "head_version_id",
            "revision",
            "inspected_version_id",
        }:
            raise InvalidContextError("current pointer fields are invalid")
        project_id = validate_identifier(current.get("project_id"), field="project_id")
        current_version_id = validate_identifier(
            current.get("version_id"), field="current_version_id"
        )
        active_branch_id = validate_identifier(
            current.get("branch_id"), field="active_branch_id"
        )
        active_head_id = validate_identifier(
            current.get("head_version_id"), field="head_version_id"
        )
        revision = current.get("revision")
        if type(revision) is not int or revision < 0:
            raise InvalidContextError("revision is invalid")
        inspected = current.get("inspected_version_id", current_version_id)
        if inspected is not None:
            inspected = validate_identifier(inspected, field="inspected_version_id")

        node_files = cls._json_files(versions_root)
        if not node_files:
            raise InvalidContextError("version nodes are missing")
        nodes: dict[str, VersionNode] = {}
        for path in node_files:
            version_id = validate_identifier(path.stem, field="version_id")
            payload = cls._read_object(path)
            node = cls._node_from_payload(payload, version_id=version_id)
            nodes[version_id] = node

        branch_files = cls._json_files(branches_root)
        if not branch_files:
            raise InvalidContextError("branch heads are missing")
        branch_heads: dict[str, str] = {}
        for path in branch_files:
            branch_id = validate_identifier(path.stem, field="branch_id")
            payload = cls._read_object(path)
            if payload.get("schema_version") != CURRENT_POINTER_SCHEMA:
                raise InvalidContextError("branch pointer schema is invalid")
            if set(payload) != {
                "schema_version",
                "project_id",
                "branch_id",
                "branch_name",
                "head_version_id",
            }:
                raise InvalidContextError("branch pointer fields are invalid")
            if validate_identifier(payload.get("project_id"), field="project_id") != project_id:
                raise InvalidContextError("branch project identity is inconsistent")
            if validate_identifier(payload.get("branch_id"), field="branch_id") != branch_id:
                raise InvalidContextError("branch pointer identity is inconsistent")
            branch_name = payload.get("branch_name")
            if not isinstance(branch_name, str) or not branch_name.strip():
                raise InvalidContextError("branch_name is invalid")
            head_id = validate_identifier(payload.get("head_version_id"), field="branch_head")
            branch_heads[branch_id] = head_id
            node = nodes.get(head_id)
            if node is None or node.branch_id != branch_id or node.branch_name != branch_name:
                raise InvalidContextError("branch head is inconsistent")

        if active_head_id != branch_heads.get(active_branch_id):
            raise InvalidContextError("current head is inconsistent")
        if current_version_id != active_head_id:
            raise InvalidContextError("current must equal active branch head")
        if inspected is not None and inspected not in nodes:
            raise InvalidContextError("inspected version is missing")
        return cls(
            project_id=project_id,
            nodes=nodes,
            branch_heads=branch_heads,
            current_version_id=current_version_id,
            active_branch_id=active_branch_id,
            inspected_version_id=inspected,
            revision=revision,
            project_root=project_root,
        )

    @classmethod
    def _resolve_create_project_root(
        cls,
        project_root: str | Path | None,
        state_path: Path | None,
    ) -> Path | None:
        if project_root is not None:
            resolved = resolve_project_root(project_root)
            if state_path is not None:
                path = cls._normalize_explicit_path(state_path)
                if path != version_context_root(resolved) / "current.json":
                    raise InvalidContextError("state path is not the canonical current pointer")
            return resolved
        if state_path is None:
            return None
        return cls._coerce_project_root(state_path)

    @staticmethod
    def _normalize_explicit_path(path_value: str | Path) -> Path:
        try:
            path = Path(path_value)
        except (TypeError, ValueError) as exc:
            raise InvalidContextError("path is invalid") from exc
        if not path.is_absolute():
            raise InvalidContextError("path must be absolute")
        return path

    @classmethod
    def _coerce_project_root(cls, value: str | Path) -> Path:
        path = cls._normalize_explicit_path(value)
        if (
            path.name == "current.json"
            and path.parent.name == "version_context"
            and path.parent.parent.name == ".review-writer"
        ):
            return resolve_project_root(path.parent.parent.parent)
        return resolve_project_root(path)

    @staticmethod
    def _require_directory(path: Path) -> None:
        if path.is_symlink() or not path.is_dir():
            raise PersistenceError("durable version-context directory is unavailable")

    @staticmethod
    def _require_file(path: Path) -> None:
        if path.is_symlink() or not path.is_file():
            raise PersistenceError("durable version-context file is unavailable")

    @classmethod
    def _json_files(cls, directory: Path) -> tuple[Path, ...]:
        try:
            entries = tuple(directory.iterdir())
        except OSError as exc:
            raise PersistenceError("durable version-context directory is unreadable") from exc
        if any(path.is_symlink() for path in entries):
            raise PersistenceError("durable version-context contains a symlink")
        if any(path.suffix != ".json" or not path.is_file() for path in entries):
            raise PersistenceError("durable version-context contains an invalid artifact")
        return tuple(sorted(entries, key=lambda path: path.name))

    @staticmethod
    def _read_object(path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise PersistenceError("durable version-context JSON is unreadable") from exc
        if not isinstance(payload, dict):
            raise InvalidContextError("durable version-context JSON must be an object")
        return payload

    @classmethod
    def _node_from_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        version_id: str,
    ) -> VersionNode:
        if payload.get("schema_version") != VERSION_CONTEXT_SCHEMA:
            raise InvalidContextError("version node schema is invalid")
        if set(payload) != {
            "schema_version",
            "version_id",
            "parent_version_id",
            "branch_id",
            "branch_name",
            "snapshot",
            "created_at",
            "snapshot_digest",
        }:
            raise InvalidContextError("version node fields are invalid")
        if payload.get("version_id") != version_id:
            raise InvalidContextError("version node identity is inconsistent")
        parent = payload.get("parent_version_id")
        if parent is not None:
            parent = validate_identifier(parent, field="parent_version_id")
        branch_id = validate_identifier(payload.get("branch_id"), field="branch_id")
        branch_name = payload.get("branch_name")
        created_at = payload.get("created_at")
        digest = payload.get("snapshot_digest")
        if not isinstance(branch_name, str) or not branch_name.strip():
            raise InvalidContextError("branch_name is invalid")
        if not isinstance(created_at, str) or not created_at.strip():
            raise InvalidContextError("created_at is invalid")
        if not isinstance(digest, str) or digest != canonical_digest(payload.get("snapshot")):
            raise InvalidContextError("snapshot digest is invalid")
        return VersionNode(
            version_id=version_id,
            parent_version_id=parent,
            branch_id=branch_id,
            branch_name=branch_name,
            snapshot=copy_snapshot(payload.get("snapshot")),
            created_at=created_at,
            snapshot_digest=digest,
        )

    @staticmethod
    def _new_node(
        *,
        version_id: str,
        parent_version_id: str | None,
        branch_id: str,
        branch_name: str,
        snapshot: Mapping[str, Any],
    ) -> VersionNode:
        version_id = validate_identifier(version_id, field="version_id")
        branch_id = validate_identifier(branch_id, field="branch_id")
        if parent_version_id is not None:
            parent_version_id = validate_identifier(
                parent_version_id, field="parent_version_id"
            )
        if not isinstance(branch_name, str) or not branch_name.strip():
            raise InvalidContextError("branch_name is invalid")
        copied = copy_snapshot(snapshot)
        try:
            digest = canonical_digest(copied)
        except (TypeError, ValueError) as exc:
            raise SnapshotInvalidError("snapshot digest cannot be computed") from exc
        return VersionNode(
            version_id=version_id,
            parent_version_id=parent_version_id,
            branch_id=branch_id,
            branch_name=branch_name,
            snapshot=copied,
            created_at=_datetime.datetime.now(_datetime.timezone.utc).isoformat(),
            snapshot_digest=digest,
        )

    def _validate_invariants(self) -> None:
        if not self._nodes:
            raise InvalidContextError("version history cannot be empty")
        if self._active_branch_id not in self._branch_heads:
            raise InvalidContextError("active branch has no head")
        active_head_id = self._branch_heads[self._active_branch_id]
        if active_head_id != self._current_version_id:
            raise InvalidContextError("current must equal active branch head")
        for version_id, node in self._nodes.items():
            if version_id != node.version_id:
                raise InvalidContextError("version node key is inconsistent")
            if node.branch_id not in self._branch_heads:
                raise InvalidContextError("version branch is missing")
            if node.parent_version_id is not None and node.parent_version_id not in self._nodes:
                raise InvalidContextError("version parent is missing")
            if node.snapshot_digest != canonical_digest(node.snapshot):
                raise InvalidContextError("version snapshot digest is invalid")
        for branch_id, head_id in self._branch_heads.items():
            validate_identifier(branch_id, field="branch_id")
            if head_id not in self._nodes:
                raise InvalidContextError("branch head is missing")
        if self._inspected_version_id is not None and self._inspected_version_id not in self._nodes:
            raise InvalidContextError("inspected version is missing")

    def _export(self) -> dict[str, Any]:
        """Export only in-memory state for transactional rollback.

        This is deliberately not the durable format.  Durable state is split
        across ``current.json``, one immutable node per version, and one branch
        head pointer per branch.
        """

        return {
            "project_id": self._project_id,
            "current_version_id": self._current_version_id,
            "active_branch_id": self._active_branch_id,
            "inspected_version_id": self._inspected_version_id,
            "revision": self._revision,
            "branch_heads": dict(sorted(self._branch_heads.items())),
            "nodes": {
                version_id: self._node_payload(node)
                for version_id, node in sorted(self._nodes.items())
            },
        }

    def _restore_export(self, payload: Mapping[str, Any]) -> None:
        raw_nodes = payload.get("nodes")
        raw_heads = payload.get("branch_heads")
        if not isinstance(raw_nodes, Mapping) or not isinstance(raw_heads, Mapping):
            raise InvalidContextError("in-memory rollback payload is invalid")
        nodes: dict[str, VersionNode] = {}
        for raw_version_id, raw_node in raw_nodes.items():
            version_id = validate_identifier(raw_version_id, field="version_id")
            if not isinstance(raw_node, Mapping):
                raise InvalidContextError("in-memory version node is invalid")
            nodes[version_id] = self._node_from_payload(raw_node, version_id=version_id)
        self._project_id = validate_identifier(payload.get("project_id"), field="project_id")
        self._nodes = nodes
        self._branch_heads = {
            validate_identifier(branch_id, field="branch_id"): validate_identifier(
                head_id, field="branch_head"
            )
            for branch_id, head_id in raw_heads.items()
        }
        self._current_version_id = validate_identifier(
            payload.get("current_version_id"), field="current_version_id"
        )
        self._active_branch_id = validate_identifier(
            payload.get("active_branch_id"), field="active_branch_id"
        )
        inspected = payload.get("inspected_version_id")
        self._inspected_version_id = (
            validate_identifier(inspected, field="inspected_version_id")
            if inspected is not None
            else None
        )
        revision = payload.get("revision")
        if type(revision) is not int or revision < 0:
            raise InvalidContextError("revision is invalid")
        self._revision = revision

    @staticmethod
    def _node_payload(node: VersionNode) -> dict[str, Any]:
        return {
            "schema_version": VERSION_CONTEXT_SCHEMA,
            **node.to_dict(),
        }

    def _current_payload(self) -> dict[str, Any]:
        return {
            "schema_version": CURRENT_POINTER_SCHEMA,
            "project_id": self._project_id,
            "version_id": self._current_version_id,
            "branch_id": self._active_branch_id,
            "head_version_id": self._branch_heads[self._active_branch_id],
            "revision": self._revision,
            "inspected_version_id": self._inspected_version_id,
        }

    def _branch_payload(self, branch_id: str) -> dict[str, Any]:
        head_id = self._branch_heads[branch_id]
        head = self._nodes[head_id]
        return {
            "schema_version": CURRENT_POINTER_SCHEMA,
            "project_id": self._project_id,
            "branch_id": branch_id,
            "branch_name": head.branch_name,
            "head_version_id": head_id,
        }

    @staticmethod
    def _json_bytes(payload: Mapping[str, Any]) -> bytes:
        try:
            return (
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    indent=2,
                )
                + "\n"
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise PersistenceError("durable version-context JSON is invalid") from exc

    @classmethod
    def _atomic_replace_bytes(cls, target: Path, content: bytes) -> None:
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise PersistenceError("durable version-context target is not an ordinary file")
        temporary: Path | None = None
        try:
            fd, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
            temporary = Path(name)
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            temporary = None
        except PersistenceError:
            raise
        except OSError as exc:
            raise PersistenceError("durable version-context write failed") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @classmethod
    def _atomic_replace_json(cls, target: Path, payload: Mapping[str, Any]) -> None:
        cls._atomic_replace_bytes(target, cls._json_bytes(payload))

    @classmethod
    def _write_new_json(cls, target: Path, payload: Mapping[str, Any]) -> None:
        if target.is_symlink() or target.exists():
            raise PersistenceError("immutable version artifact already exists")
        content = cls._json_bytes(payload)
        file_descriptor: int | None = None
        created = False
        completed = False
        try:
            file_descriptor = os.open(
                target,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o644,
            )
            created = True
            with os.fdopen(file_descriptor, "wb") as handle:
                file_descriptor = None
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            completed = True
        except PersistenceError:
            raise
        except OSError as exc:
            raise PersistenceError("immutable version artifact write failed") from exc
        finally:
            if file_descriptor is not None:
                os.close(file_descriptor)
            if created and not completed and target.exists():
                target.unlink(missing_ok=True)

    def _persist_create(self) -> None:
        if self._project_root is None or self._context_root is None:
            return
        root = self._context_root
        if root.exists() or root.is_symlink():
            raise PersistenceError("project version-context already exists")
        try:
            root.parent.mkdir(parents=True, exist_ok=True)
            self._require_directory(root.parent)
            root.mkdir()
            versions_root = root / "versions"
            branches_root = root / "branches"
            versions_root.mkdir()
            branches_root.mkdir()
            self._write_new_json(
                versions_root / f"{self._current_version_id}.json",
                self._node_payload(self._nodes[self._current_version_id]),
            )
            self._atomic_replace_json(
                branches_root / f"{self._active_branch_id}.json",
                self._branch_payload(self._active_branch_id),
            )
            # The current pointer is intentionally written last.
            self._atomic_replace_json(root / "current.json", self._current_payload())
        except PersistenceError:
            raise
        except OSError as exc:
            raise PersistenceError("project version-context creation failed") from exc

    def _assert_durable_state_matches(self, previous: Mapping[str, Any]) -> None:
        if self._project_root is None:
            return
        live = type(self).load(self._project_root)
        previous_nodes = previous.get("nodes")
        if not isinstance(previous_nodes, Mapping):
            raise InvalidContextError("in-memory version history is invalid")
        previous_digests = {
            version_id: raw_node.get("snapshot_digest")
            for version_id, raw_node in previous_nodes.items()
            if isinstance(raw_node, Mapping)
        }
        live_digests = {
            version_id: node.snapshot_digest for version_id, node in live._nodes.items()
        }
        if (
            live._project_id != self._project_id
            or live._revision != previous.get("revision")
            or live._current_version_id != previous.get("current_version_id")
            or live._active_branch_id != previous.get("active_branch_id")
            or live._branch_heads != previous.get("branch_heads")
            or live_digests != previous_digests
        ):
            raise StaleRevisionError("durable version-context is stale")

    def _persist_transition(self, previous: Mapping[str, Any]) -> None:
        if self._project_root is None or self._context_root is None:
            return
        self._assert_durable_state_matches(previous)
        versions_root = self._context_root / "versions"
        branches_root = self._context_root / "branches"
        previous_nodes = previous["nodes"]
        previous_heads = previous["branch_heads"]
        written_nodes: list[Path] = []
        previous_branch_bytes: dict[Path, bytes | None] = {}
        try:
            for version_id, node in self._nodes.items():
                if version_id in previous_nodes:
                    continue
                path = versions_root / f"{version_id}.json"
                self._write_new_json(path, self._node_payload(node))
                written_nodes.append(path)

            for branch_id, head_id in self._branch_heads.items():
                if previous_heads.get(branch_id) == head_id:
                    continue
                path = branches_root / f"{branch_id}.json"
                previous_branch_bytes[path] = path.read_bytes() if path.exists() else None
                self._atomic_replace_json(path, self._branch_payload(branch_id))

            # This is the sole current pointer write and is always last.
            self._atomic_replace_json(self._context_root / "current.json", self._current_payload())
        except BaseException as exc:
            for path, content in previous_branch_bytes.items():
                try:
                    if content is None:
                        path.unlink(missing_ok=True)
                    else:
                        self._atomic_replace_bytes(path, content)
                except OSError:
                    pass
            for path in written_nodes:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            if isinstance(exc, ProductFoundationError):
                raise
            raise PersistenceError("project version-context transition failed") from exc

    def _persist(self) -> None:
        """Compatibility hook for callers inside this module."""

        self._persist_transition(self._export())

    def _commit(self, mutation: Callable[[], Any], *, bump_revision: bool = True) -> Any:
        previous = self._export()
        try:
            result = mutation()
            if bump_revision:
                self._revision += 1
            self._validate_invariants()
            self._persist_transition(previous)
            return result
        except BaseException:
            self._restore_export(previous)
            raise

    def _require_node(self, version_id: str) -> VersionNode:
        version_id = validate_identifier(version_id, field="version_id")
        try:
            return self._nodes[version_id]
        except KeyError as exc:
            raise VersionNotFoundError(version_id) from exc

    def _public_node(self, node: VersionNode) -> VersionNode:
        return VersionNode(
            version_id=node.version_id,
            parent_version_id=node.parent_version_id,
            branch_id=node.branch_id,
            branch_name=node.branch_name,
            snapshot=copy_snapshot(node.snapshot),
            created_at=node.created_at,
            snapshot_digest=node.snapshot_digest,
        )

    def _view(self, node: VersionNode) -> VersionView:
        is_active_head = (
            node.version_id == self._branch_heads.get(self._active_branch_id)
            and node.branch_id == self._active_branch_id
        )
        is_current = node.version_id == self._current_version_id
        can_write = is_active_head and is_current
        return VersionView(
            version_id=node.version_id,
            parent_version_id=node.parent_version_id,
            branch_id=node.branch_id,
            branch_name=node.branch_name,
            snapshot=copy_snapshot(node.snapshot),
            snapshot_digest=node.snapshot_digest,
            read_only=not can_write,
            can_write=can_write,
            is_current=is_current,
            is_active_head=is_active_head,
        )

    def state(self) -> ContextState:
        with self._lock:
            return ContextState(
                project_id=self._project_id,
                current_version_id=self._current_version_id,
                active_branch_id=self._active_branch_id,
                active_head_id=self._branch_heads[self._active_branch_id],
                writable_version_id=self._branch_heads[self._active_branch_id],
                inspected_version_id=self._inspected_version_id,
                revision=self._revision,
                branch_heads=dict(self._branch_heads),
            )

    @property
    def current_instance(self) -> ContextState:
        return self.state()

    def view_version(self, version_id: str) -> VersionView:
        with self._lock:
            return self._view(self._require_node(version_id))

    view = view_version

    def select_version(self, version_id: str) -> VersionView:
        with self._lock:
            self._require_node(version_id)

            def mutation() -> VersionView:
                self._inspected_version_id = version_id
                return self._view(self._nodes[version_id])

            return self._commit(mutation, bump_revision=False)

    inspect = select_version

    def history(self) -> tuple[VersionView, ...]:
        with self._lock:
            return tuple(self._view(self._nodes[version_id]) for version_id in self._nodes)

    versions = history

    def compare_versions(self, left_version_id: str, right_version_id: str) -> VersionComparison:
        with self._lock:
            left = self._require_node(left_version_id)
            right = self._require_node(right_version_id)
            keys = sorted(set(left.snapshot) | set(right.snapshot))
            changes: dict[str, dict[str, Any]] = {}
            for key in keys:
                left_value = left.snapshot.get(key)
                right_value = right.snapshot.get(key)
                if left_value != right_value or (key not in left.snapshot) != (key not in right.snapshot):
                    changes[key] = {"left": copy_value(left_value), "right": copy_value(right_value)}
            return VersionComparison(
                left_version_id=left.version_id,
                right_version_id=right.version_id,
                changed_fields=tuple(changes),
                changes=changes,
            )

    compare = compare_versions

    def download_version(self, version_id: str) -> DownloadArtifact:
        with self._lock:
            node = self._require_node(version_id)
            payload = {
                "schema_version": "review-writer.version-download.v1",
                "project_id": self._project_id,
                "version_id": node.version_id,
                "parent_version_id": node.parent_version_id,
                "branch_id": node.branch_id,
                "branch_name": node.branch_name,
                "snapshot_digest": node.snapshot_digest,
                "snapshot": copy_snapshot(node.snapshot),
            }
            content = (
                json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2)
                + "\n"
            ).encode("utf-8")
            return DownloadArtifact(
                version_id=node.version_id,
                filename=f"review-{node.version_id}.json",
                media_type="application/json",
                content=content,
                metadata={
                    "project_id": self._project_id,
                    "branch_id": node.branch_id,
                    "snapshot_digest": node.snapshot_digest,
                    "read_only": True,
                },
            )

    download = download_version

    def _check_revision(self, expected_revision: int | None) -> None:
        if expected_revision is not None and expected_revision != self._revision:
            raise StaleRevisionError()

    def publish_active_head(
        self,
        snapshot: Mapping[str, Any],
        *,
        expected_head_id: str,
        expected_revision: int | None = None,
        version_id: str | None = None,
    ) -> VersionNode:
        with self._lock:
            self._check_revision(expected_revision)
            expected_head_id = validate_identifier(expected_head_id, field="expected_head_id")
            if expected_head_id != self._branch_heads[self._active_branch_id]:
                if self._project_root is None and expected_head_id in self._nodes:
                    raise ReadOnlyVersionError()
                raise StaleRevisionError("active head is no longer current")
            current = self._nodes[self._current_version_id]
            new_version_id = version_id or uuid.uuid4().hex
            new_node = self._new_node(
                version_id=new_version_id,
                parent_version_id=current.version_id,
                branch_id=self._active_branch_id,
                branch_name=current.branch_name,
                snapshot=snapshot,
            )
            if new_node.version_id in self._nodes:
                raise BranchConflictError("version_id already exists")

            def mutation() -> VersionNode:
                self._nodes[new_node.version_id] = new_node
                self._branch_heads[self._active_branch_id] = new_node.version_id
                self._current_version_id = new_node.version_id
                self._inspected_version_id = new_node.version_id
                return self._public_node(new_node)

            return self._commit(mutation)

    publish_version = publish_active_head
    edit_active_head = publish_active_head

    def preview_branch(
        self,
        source_version_id: str,
        *,
        branch_id: str | None = None,
        branch_name: str | None = None,
        version_id: str | None = None,
        activate: bool = False,
    ) -> BranchPreview:
        with self._lock:
            source = self._require_node(source_version_id)
            new_branch_id = branch_id or f"branch-{uuid.uuid4().hex}"
            new_version_id = version_id or uuid.uuid4().hex
            new_branch_id = validate_identifier(new_branch_id, field="branch_id")
            new_version_id = validate_identifier(new_version_id, field="version_id")
            if new_branch_id in self._branch_heads or new_version_id in self._nodes:
                raise BranchConflictError()
            name = branch_name or f"Branch from {source.version_id}"
            if not isinstance(name, str) or not name.strip():
                raise InvalidContextError("branch_name is invalid")
            return BranchPreview(
                source_version_id=source.version_id,
                new_branch_id=new_branch_id,
                branch_name=name,
                new_version_id=new_version_id,
                current_version_id=self._current_version_id,
                active_branch_id=self._active_branch_id,
                activates_branch=activate,
            )

    def branch_from(
        self,
        source_version_id: str,
        *,
        branch_id: str | None = None,
        branch_name: str | None = None,
        version_id: str | None = None,
        confirm: bool = False,
        activate: bool = False,
        expected_revision: int | None = None,
    ) -> VersionNode:
        with self._lock:
            preview = self.preview_branch(
                source_version_id,
                branch_id=branch_id,
                branch_name=branch_name,
                version_id=version_id,
                activate=activate,
            )
            if not confirm:
                raise ConfirmationRequiredError(preview)
            self._check_revision(expected_revision)
            source = self._nodes[preview.source_version_id]
            new_node = self._new_node(
                version_id=preview.new_version_id,
                parent_version_id=source.version_id,
                branch_id=preview.new_branch_id,
                branch_name=preview.branch_name,
                snapshot=source.snapshot,
            )

            def mutation() -> VersionNode:
                self._nodes[new_node.version_id] = new_node
                self._branch_heads[new_node.branch_id] = new_node.version_id
                if activate:
                    self._active_branch_id = new_node.branch_id
                    self._current_version_id = new_node.version_id
                    self._inspected_version_id = new_node.version_id
                return self._public_node(new_node)

            return self._commit(mutation)

    create_branch = branch_from

    def activate_branch(
        self,
        branch_id: str,
        *,
        expected_head_id: str | None = None,
        confirm: bool = False,
        expected_revision: int | None = None,
    ) -> ContextState:
        with self._lock:
            branch_id = validate_identifier(branch_id, field="branch_id")
            head_id = self._branch_heads.get(branch_id)
            if head_id is None:
                raise VersionNotFoundError(branch_id)
            if not confirm:
                raise ConfirmationRequiredError(
                    {"branch_id": branch_id, "head_version_id": head_id}
                )
            self._check_revision(expected_revision)
            if expected_head_id is not None and expected_head_id != head_id:
                raise StaleRevisionError()

            def mutation() -> ContextState:
                self._active_branch_id = branch_id
                self._current_version_id = head_id
                self._inspected_version_id = head_id
                return self.state()

            self._commit(mutation)
            return self.state()

    def preview_undo(self, target_version_id: str | None = None) -> UndoPreview:
        with self._lock:
            current = self._nodes[self._current_version_id]
            if current.parent_version_id is None:
                raise UndoUnavailableError()
            target_id = target_version_id or current.parent_version_id
            target = self._require_node(target_id)
            if target.version_id == current.version_id:
                raise UndoTargetError("undo target is current")
            if target.branch_id != current.branch_id:
                raise UndoTargetError("undo cannot cross branch lineage")
            discarded: list[str] = []
            cursor = current
            while cursor.version_id != target.version_id:
                discarded.append(cursor.version_id)
                if cursor.parent_version_id is None:
                    raise UndoTargetError("undo target is not an ancestor")
                cursor = self._nodes[cursor.parent_version_id]
            return UndoPreview(
                current_version_id=current.version_id,
                target_version_id=target.version_id,
                discarded_version_ids=tuple(discarded),
                active_branch_id=self._active_branch_id,
            )

    def undo(
        self,
        target_version_id: str | None = None,
        *,
        confirm: bool = False,
        expected_revision: int | None = None,
    ) -> ContextState:
        if self._project_root is not None:
            return self.rollback(
                target_version_id,
                confirm=confirm,
                expected_revision=expected_revision,
            )
        with self._lock:
            preview = self.preview_undo(target_version_id)
            if not confirm:
                raise ConfirmationRequiredError(preview)
            self._check_revision(expected_revision)

            def mutation() -> ContextState:
                self._branch_heads[self._active_branch_id] = preview.target_version_id
                self._current_version_id = preview.target_version_id
                self._inspected_version_id = preview.target_version_id
                return self.state()

            self._commit(mutation)
            return self.state()

    def rollback(
        self,
        target_version_id: str | None = None,
        *,
        confirm: bool = False,
        expected_revision: int | None = None,
        expected_head_id: str | None = None,
        branch_id: str | None = None,
        branch_name: str | None = None,
        version_id: str | None = None,
    ) -> ContextState:
        """Create a new writable rollback leaf on a new branch.

        Historical nodes and the original branch head remain immutable.  The
        new branch is activated only by this explicit confirmed operation.
        """

        with self._lock:
            preview = self.preview_undo(target_version_id)
            self._check_revision(expected_revision)
            current_head_id = self._branch_heads[self._active_branch_id]
            if expected_head_id is not None:
                expected_head_id = validate_identifier(
                    expected_head_id,
                    field="expected_head_id",
                )
                if expected_head_id != current_head_id:
                    raise StaleRevisionError("active head is no longer current")
            new_branch_id = validate_identifier(
                branch_id or f"rollback-{uuid.uuid4().hex}",
                field="branch_id",
            )
            new_version_id = validate_identifier(
                version_id or uuid.uuid4().hex,
                field="version_id",
            )
            if new_branch_id in self._branch_heads or new_version_id in self._nodes:
                raise BranchConflictError()
            current = self._nodes[self._current_version_id]
            new_branch_name = branch_name or f"Rollback from {current.version_id}"
            if not isinstance(new_branch_name, str) or not new_branch_name.strip():
                raise InvalidContextError("branch_name is invalid")
            if not confirm:
                raise ConfirmationRequiredError(preview)
            target = self._nodes[preview.target_version_id]
            new_node = self._new_node(
                version_id=new_version_id,
                parent_version_id=target.version_id,
                branch_id=new_branch_id,
                branch_name=new_branch_name,
                snapshot=target.snapshot,
            )

            def mutation() -> ContextState:
                self._nodes[new_node.version_id] = new_node
                self._branch_heads[new_node.branch_id] = new_node.version_id
                self._active_branch_id = new_node.branch_id
                self._current_version_id = new_node.version_id
                self._inspected_version_id = new_node.version_id
                return self.state()

            self._commit(mutation)
            return self.state()

    rollback_preview = preview_undo
    undo_preview = preview_undo
