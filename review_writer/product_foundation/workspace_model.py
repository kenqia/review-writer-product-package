"""User-facing seven-workspace navigation and version context projection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .contracts import InvalidContextError

if TYPE_CHECKING:
    from .service import VersionContext


@dataclass(frozen=True)
class WorkspaceDefinition:
    workspace_id: str
    label: str
    description: str
    children: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.workspace_id,
            "label": self.label,
            "description": self.description,
        }
        if self.children:
            result["children"] = list(self.children)
        return result


@dataclass(frozen=True)
class WorkspaceVersionContext:
    workspace_id: str
    project_id: str
    current_version_id: str
    inspected_version_id: str
    branch_id: str
    head_version_id: str
    read_only: bool
    can_write: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace_id,
            "project_id": self.project_id,
            "current_version": self.current_version_id,
            "inspected_version": self.inspected_version_id,
            "branch": self.branch_id,
            "head": self.head_version_id,
            "read_only": self.read_only,
            "can_write": self.can_write,
        }


class WorkspaceModel:
    """A small, stable user information architecture for future UI adapters."""

    _TOP_LEVEL = (
        WorkspaceDefinition("Overview", "Overview", "Project overview and next actions"),
        WorkspaceDefinition(
            "Research",
            "Research",
            "Sources, evidence, comparisons, and synthesis",
            ("Scope", "Corpus", "Evidence", "Matrix", "Synthesis"),
        ),
        WorkspaceDefinition("Draft", "Draft", "Read and revise the review document"),
        WorkspaceDefinition("Figures", "Figures", "Plan and inspect figures and tables"),
        WorkspaceDefinition("Review", "Review", "Review findings and revisions"),
        WorkspaceDefinition("History", "History", "View, compare, download, and branch versions"),
        WorkspaceDefinition("Release", "Release", "Inspect version-bound delivery outputs"),
    )

    @classmethod
    def default(cls) -> "WorkspaceModel":
        return cls()

    @property
    def workspace_ids(self) -> tuple[str, ...]:
        return tuple(item.workspace_id for item in self._TOP_LEVEL)

    @property
    def research_workspace_ids(self) -> tuple[str, ...]:
        return self._TOP_LEVEL[1].children

    def workspace(self, workspace_id: str) -> WorkspaceDefinition:
        all_ids = set(self.workspace_ids) | set(self.research_workspace_ids)
        if workspace_id not in all_ids:
            raise InvalidContextError("workspace is invalid")
        for definition in self._TOP_LEVEL:
            if definition.workspace_id == workspace_id:
                return definition
            if workspace_id in definition.children:
                return WorkspaceDefinition(
                    workspace_id=workspace_id,
                    label=workspace_id,
                    description=f"{workspace_id} research workspace",
                )
        raise InvalidContextError("workspace is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {"workspaces": [item.to_dict() for item in self._TOP_LEVEL]}

    def bind_version_context(
        self,
        context: "VersionContext",
        *,
        workspace_id: str = "Overview",
    ) -> WorkspaceVersionContext:
        self.workspace(workspace_id)
        state = context.state()
        inspected_version_id = state.inspected_version_id or state.current_version_id
        inspected = context.view_version(inspected_version_id)
        return WorkspaceVersionContext(
            workspace_id=workspace_id,
            project_id=state.project_id,
            current_version_id=state.current_version_id,
            inspected_version_id=inspected.version_id,
            branch_id=state.active_branch_id,
            head_version_id=state.active_head_id,
            read_only=inspected.read_only,
            can_write=inspected.can_write,
        )

    context_for = bind_version_context


def default_workspace_model() -> WorkspaceModel:
    return WorkspaceModel.default()
