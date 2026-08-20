"""Fail-closed import and lineage for MinerU Chemical Paper manual exports.

The original project PDF remains authoritative.  This module never extracts an
archive to disk, contacts MinerU, or synthesizes a missing chemical field.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
from math import isfinite
import os
import re
import secrets
import stat
import tempfile
import unicodedata
import zipfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

from jsonschema import Draft202012Validator

from .paper_evidence_store import PaperEvidenceStoreError, project_write_lock
from .source_truth import (
    ProjectSourceIndex,
    REPO_ROOT,
    SourceTruthError,
    acquisition_receipt_digest,
    build_project_source_index,
    canonical_digest,
    declared_study_ids,
    load_source_truth_bundle,
    project_source_binding,
    source_truth_asset,
)


STATE_ROOT = Path("01_evidence/chemical_paper")
STATE_NAME = "state.json"
STATE_SCHEMA = REPO_ROOT / "schemas/evidence/chemical_paper_state.v2.schema.json"
PROVENANCE_SMILES_FIELDS = ("smiles_expanded", "smiles_unexpanded")
FIELD_NAMES = ("mol_idt", "resolved_smiles")
REQUIRED_FIELD_NAMES = frozenset((*FIELD_NAMES, "elements"))
RESOLVED_SMILES_STATUSES = frozenset({"CONFIRMED", "AI_PROVISIONAL", "BLOCKED"})
RESEARCHER_SAFE_PROVENANCE_KEYS = frozenset({"kind", "source", "source_field"})
CORE_MOLECULE_COUNT = 309
CORE_COVERAGE_THRESHOLD = 0.8
ELEMENT_REVIEW_STATES = frozenset({"not_reviewed", "confirmed", "corrected", "not_applicable"})
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_ENTRY_COUNT = 16
MAX_ENTRY_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200.0
NESTED_ARCHIVE_SUFFIXES = frozenset({".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz"})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ID = re.compile(r"^(?!\.\.?$)(?!.*[/\\\x00\r\n])\S{1,240}$")
_SMILES_ORGANIC_ATOMS = frozenset({"B", "C", "N", "O", "P", "S", "F", "Cl", "Br", "I", "*"})
_SMILES_AROMATIC_ATOMS = frozenset({"b", "c", "n", "o", "p", "s", "se", "as"})
_SMILES_BONDS = frozenset("-=#:~/\\")
_ELEMENT = re.compile(r"^[A-Z][a-z]?$|^\*$")
_ELEMENTS = frozenset(
    "H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu Zn "
    "Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe Cs Ba La Ce "
    "Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt Au Hg Tl Pb Bi Po At Rn "
    "Fr Ra Ac Th Pa U Np Pu Am Cm Bk Cf Es Fm Md No Lr Rf Db Sg Bh Hs Mt Ds Rg Cn Nh Fl Mc "
    "Lv Ts Og".split()
)


class ChemicalPaperError(ValueError):
    """Stable fail-closed Chemical Paper error."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ChemicalPaperPdfLocatorDescriptor:
    """Exact current Chemical PDF binding for snapshot-and-reverify consumers."""

    project_root: Path
    project_device: int
    project_inode: int
    study_id: str
    source_id: str
    binding: str
    source_truth_bundle_digest: str
    pdf_sha256: str
    pdf_size_bytes: int
    asset_path: Path
    page_count: int


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _identifier(value: object, code: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ChemicalPaperError(code)
    return value


def _digest(value: object, code: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ChemicalPaperError(code)
    return value


def _actor(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"actor_type", "actor_label"}:
        raise ChemicalPaperError("ACTOR_INVALID")
    actor_type = value.get("actor_type")
    actor_label = value.get("actor_label")
    if actor_type not in {"human_researcher", "simulated_researcher_agent"}:
        raise ChemicalPaperError("ACTOR_INVALID")
    if not isinstance(actor_label, str) or actor_label != actor_label.strip() or not actor_label or len(actor_label) > 200:
        raise ChemicalPaperError("ACTOR_INVALID")
    return {"actor_type": actor_type, "actor_label": actor_label}


def _reason(value: object) -> str:
    if not isinstance(value, str) or value != value.strip() or not value or len(value) > 2000:
        raise ChemicalPaperError("REASON_INVALID")
    return value


def _smiles_bracket_atom_end(value: str, index: int) -> int | None:
    cursor = index + 1
    if cursor >= len(value):
        return None
    isotope_start = cursor
    while cursor < len(value) and value[cursor].isdigit():
        cursor += 1
    if cursor == isotope_start and value[cursor] == "*":
        cursor += 1
    else:
        if cursor >= len(value):
            return None
        if value[cursor].islower():
            element = value[cursor : cursor + 2]
            if element not in _SMILES_AROMATIC_ATOMS:
                element = value[cursor]
            if element not in _SMILES_AROMATIC_ATOMS:
                return None
            cursor += len(element)
        else:
            if not value[cursor].isupper():
                return None
            element = value[cursor]
            cursor += 1
            if cursor < len(value) and value[cursor].islower():
                element += value[cursor]
                cursor += 1
            if element not in _ELEMENTS:
                return None
    if cursor < len(value) and value[cursor] == "@":
        cursor += 1
        if cursor < len(value) and value[cursor] == "@":
            cursor += 1
        # Extended tetrahedral/stereo descriptors are @TH1, @AL1, @SP1,
        # @TB1 and @OH1.  Consume only a complete descriptor when present.
        for descriptor in ("TH", "AL", "SP", "TB", "OH"):
            if value.startswith(descriptor, cursor):
                cursor += len(descriptor)
                if cursor >= len(value) or not value[cursor].isdigit():
                    return None
                while cursor < len(value) and value[cursor].isdigit():
                    cursor += 1
                break
    if cursor < len(value) and value[cursor] == "H":
        cursor += 1
        while cursor < len(value) and value[cursor].isdigit():
            cursor += 1
    if cursor < len(value) and value[cursor] in "+-":
        sign = value[cursor]
        cursor += 1
        if cursor < len(value) and value[cursor] == sign:
            while cursor < len(value) and value[cursor] == sign:
                cursor += 1
        else:
            charge_start = cursor
            while cursor < len(value) and value[cursor].isdigit():
                cursor += 1
            charge_digits = value[charge_start:cursor]
            if charge_digits and set(charge_digits) == {"0"}:
                return None
    if cursor < len(value) and value[cursor] == ":":
        cursor += 1
        map_start = cursor
        while cursor < len(value) and value[cursor].isdigit():
            cursor += 1
        if cursor == map_start:
            return None
    if cursor >= len(value) or value[cursor] != "]":
        return None
    return cursor + 1


def _smiles_atom_end(value: str, index: int) -> int | None:
    if value[index] == "[":
        return _smiles_bracket_atom_end(value, index)
    if value.startswith("Cl", index) or value.startswith("Br", index):
        return index + 2
    if value.startswith("se", index) or value.startswith("as", index):
        return index + 2
    if value[index] in _SMILES_ORGANIC_ATOMS or value[index] in {"b", "c", "n", "o", "p", "s"}:
        return index + 1
    return None


def _valid_resolved_smiles(value: str) -> bool:
    if not isinstance(value, str) or not value or value != value.strip():
        return False
    cursor = 0
    atom_count = 0
    component_has_atom = False
    previous_atom = False
    pending_bond: str | None = None
    branch_stack: list[tuple[int, bool]] = []
    ring_bonds: dict[str, tuple[int, str | None]] = {}
    while cursor < len(value):
        token = value[cursor]
        if token == ".":
            if (
                not component_has_atom
                or not previous_atom
                or pending_bond is not None
                or branch_stack
                or ring_bonds
            ):
                return False
            component_has_atom = False
            previous_atom = False
            cursor += 1
            continue
        if token == "(":
            if (
                not previous_atom
                or pending_bond is not None
                or (branch_stack and not branch_stack[-1][1])
            ):
                return False
            branch_stack.append((atom_count, False))
            cursor += 1
            continue
        if token == ")":
            if not branch_stack or not previous_atom or pending_bond is not None:
                return False
            start_atom_count, has_atom = branch_stack[-1]
            if not has_atom or atom_count <= start_atom_count:
                return False
            branch_stack.pop()
            previous_atom = True
            cursor += 1
            continue
        if token in _SMILES_BONDS:
            if not previous_atom or pending_bond is not None:
                return False
            pending_bond = token
            cursor += 1
            continue
        if token.isdigit() or token == "%":
            if not previous_atom:
                return False
            if token == "%":
                if cursor + 2 >= len(value) or not value[cursor + 1 : cursor + 3].isdigit():
                    return False
                ring_label = value[cursor + 1 : cursor + 3]
                cursor += 3
            else:
                ring_label = token
                cursor += 1
            if ring_label in ring_bonds:
                opening_atom, prior_bond = ring_bonds.pop(ring_label)
                if opening_atom == atom_count:
                    return False
                if prior_bond is not None and pending_bond is not None and prior_bond != pending_bond:
                    return False
            else:
                ring_bonds[ring_label] = (atom_count, pending_bond)
            pending_bond = None
            continue
        atom_end = _smiles_atom_end(value, cursor)
        if atom_end is None:
            return False
        atom_count += 1
        component_has_atom = True
        previous_atom = True
        pending_bond = None
        if branch_stack:
            branch_stack[-1] = (branch_stack[-1][0], True)
        cursor = atom_end
    return bool(atom_count and component_has_atom and previous_atom and pending_bond is None and not branch_stack and not ring_bonds)


def _first_valid_smiles_candidate(
    expanded: str | None,
    unexpanded: str | None,
) -> tuple[str | None, str | None]:
    for field, candidate in (
        ("smiles_expanded", expanded),
        ("smiles_unexpanded", unexpanded),
    ):
        if candidate is not None and _valid_resolved_smiles(candidate):
            return candidate, field
    return None, None


def _correction_value(
    field: str, value: object, resolution_status: object = None, gap_reason: object = None
) -> str | None:
    if resolution_status == "BLOCKED":
        if field != "resolved_smiles" or value is not None:
            raise ChemicalPaperError("BLOCKED_VALUE_MUST_BE_NULL")
        if (
            not isinstance(gap_reason, str)
            or not gap_reason.strip()
            or gap_reason != gap_reason.strip()
        ):
            raise ChemicalPaperError("GAP_REASON_REQUIRED")
        return None
    if not isinstance(value, str) or value != value.strip() or not value or len(value) > 20000:
        raise ChemicalPaperError("CHEMICAL_FIELD_VALUE_INVALID")
    if field == "resolved_smiles" and not _valid_resolved_smiles(value):
        raise ChemicalPaperError("SMILES_INVALID")
    return value


def _correction_locator(value: object, page_count: int) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - {"page", "figure_label", "bbox"}:
        raise ChemicalPaperError("PDF_LOCATOR_INVALID")
    page = value.get("page")
    if not isinstance(page, int) or isinstance(page, bool) or not 1 <= page <= page_count:
        raise ChemicalPaperError("PDF_LOCATOR_INVALID")
    label = value.get("figure_label")
    if label is not None and (
        not isinstance(label, str) or not label.strip() or label != label.strip()
    ):
        raise ChemicalPaperError("PDF_LOCATOR_INVALID")
    bbox = value.get("bbox")
    if bbox is not None and (
        not isinstance(bbox, list)
        or len(bbox) != 4
        or not all(
            isinstance(item, (int, float)) and not isinstance(item, bool)
            for item in bbox
        )
    ):
        raise ChemicalPaperError("PDF_LOCATOR_INVALID")
    return copy.deepcopy(value)


def _resolution_provenance(value: object) -> object:
    """Validate the small, safe provenance payload carried by new resolutions."""

    if isinstance(value, str):
        if not value.strip() or value != value.strip() or len(value) > 2000:
            raise ChemicalPaperError("RESOLUTION_PROVENANCE_INVALID")
        return value
    if not isinstance(value, dict) or not value:
        raise ChemicalPaperError("RESOLUTION_PROVENANCE_INVALID")
    for key, item in value.items():
        if (
            not isinstance(key, str)
            or not key.strip()
            or key != key.strip()
            or len(key) > 100
            or not isinstance(item, (str, int, float, bool, type(None)))
            or isinstance(item, float) and not isfinite(item)
        ):
            raise ChemicalPaperError("RESOLUTION_PROVENANCE_INVALID")
        if isinstance(item, str) and len(item) > 2000:
            raise ChemicalPaperError("RESOLUTION_PROVENANCE_INVALID")
    return copy.deepcopy(value)


def _researcher_safe_provenance(value: object) -> dict[str, str]:
    """Keep only non-sensitive provenance labels in researcher-facing views."""

    safe: dict[str, str] = {}
    if isinstance(value, dict):
        for key in RESEARCHER_SAFE_PROVENANCE_KEYS:
            item = value.get(key)
            if not isinstance(item, str) or item != item.strip() or not item or len(item) > 200:
                continue
            lowered = item.casefold()
            if (
                "/" in item
                or "\\" in item
                or _SHA256.fullmatch(lowered)
                or any(marker in lowered for marker in ("token", "sha256", "digest", "secret", "cookie", "auth"))
            ):
                continue
            safe[key] = item
    return safe or {"kind": "provenance_redacted"}


def _resolution_metadata(
    status: object,
    confidence: object,
    provenance: object,
    actor: dict[str, str],
    gap_reason: object = None,
) -> dict[str, Any]:
    if status is None:
        if confidence is not None or provenance is not None or gap_reason is not None:
            raise ChemicalPaperError("RESOLUTION_STATUS_REQUIRED")
        return {}
    if status not in {"CONFIRMED", "AI_PROVISIONAL", "BLOCKED"}:
        raise ChemicalPaperError("RESOLUTION_STATUS_INVALID")
    if status == "BLOCKED":
        if (
            not isinstance(gap_reason, str)
            or not gap_reason.strip()
            or gap_reason != gap_reason.strip()
            or len(gap_reason) > 2000
            or confidence is not None
            or provenance is not None
        ):
            raise ChemicalPaperError("GAP_REASON_REQUIRED")
        return {"resolution_status": "BLOCKED", "gap_reason": gap_reason}
    if status == "CONFIRMED":
        if actor["actor_type"] != "human_researcher":
            raise ChemicalPaperError("RESEARCHER_CONFIRMATION_REQUIRED")
        if confidence is not None or provenance is not None or gap_reason is not None:
            raise ChemicalPaperError("RESOLUTION_METADATA_NOT_ALLOWED")
        return {"resolution_status": "CONFIRMED"}
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not isfinite(float(confidence))
        or not 0 <= float(confidence) <= 1
    ):
        raise ChemicalPaperError("RESOLUTION_METADATA_REQUIRED")
    return {
        "resolution_status": "AI_PROVISIONAL",
        "confidence": float(confidence),
        "provenance": _resolution_provenance(provenance),
    }


def _actor_provenance_mismatch(actor: object) -> bool:
    if not isinstance(actor, dict):
        return False
    actor_type = actor.get("actor_type")
    actor_label = actor.get("actor_label")
    if not isinstance(actor_type, str) or not isinstance(actor_label, str):
        return False
    label = actor_label.casefold()
    return (
        actor_type == "human_researcher"
        and ("simulated" in label or "agent" in label)
    ) or (
        actor_type == "simulated_researcher_agent"
        and ("human" in label or "researcher" == label)
    )


def _project(project: Path) -> Path:
    supplied = Path(project)
    if supplied.is_symlink() or not supplied.is_dir():
        raise ChemicalPaperError("PROJECT_INVALID")
    try:
        root = supplied.resolve(strict=True)
    except OSError as exc:
        raise ChemicalPaperError("PROJECT_INVALID") from exc
    path = root / STATE_ROOT
    if os.path.lexists(path) and (path.is_symlink() or not path.is_dir()):
        raise ChemicalPaperError("CHEMICAL_PAPER_PATH_INVALID")
    return root


def _project_instance_identity(project_root: Path) -> tuple[int, int]:
    try:
        metadata = os.stat(project_root, follow_symlinks=False)
    except OSError as exc:
        raise ChemicalPaperError("PROJECT_INVALID") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise ChemicalPaperError("PROJECT_INVALID")
    return metadata.st_dev, metadata.st_ino


def _state_path(project: Path, study_id: str) -> Path:
    return project / STATE_ROOT / _identifier(study_id, "STUDY_ID_INVALID") / STATE_NAME


def _json_bytes(value: object) -> bytes:
    try:
        return (json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ChemicalPaperError("CHEMICAL_PAPER_STATE_INVALID") from exc


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink() or (os.path.lexists(path) and (path.is_symlink() or not path.is_file())):
        raise ChemicalPaperError("CHEMICAL_PAPER_PATH_INVALID")
    temporary: Path | None = None
    try:
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(fd, "wb") as handle:
            handle.write(_json_bytes(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise ChemicalPaperError("CHEMICAL_PAPER_WRITE_FAILED") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _canonical_state_digest(state: dict[str, Any]) -> str:
    return canonical_digest({key: value for key, value in state.items() if key != "state_digest"})


def _version_token(state: dict[str, Any]) -> str:
    encoded = base64.urlsafe_b64encode(bytes.fromhex(state["state_digest"])).decode("ascii").rstrip("=")
    return f"cpv2.{encoded}"


def _validate_event_chain(
    rows: list[dict[str, Any]], *, event_key: str, prior_key: str
) -> tuple[str | None, list[dict[str, Any]]]:
    by_event: dict[str, dict[str, Any]] = {}
    by_prior: dict[str | None, dict[str, Any]] = {}
    for row in rows:
        expected = canonical_digest(
            {key: value for key, value in row.items() if key != event_key}
        )
        event_digest = row.get(event_key)
        prior_digest = row.get(prior_key)
        if (
            event_digest != expected
            or not isinstance(event_digest, str)
            or event_digest in by_event
            or prior_digest in by_prior
        ):
            raise ChemicalPaperError("CHEMICAL_PAPER_HISTORY_INVALID")
        by_event[event_digest] = row
        by_prior[prior_digest] = row
    if rows and None not in by_prior:
        raise ChemicalPaperError("CHEMICAL_PAPER_HISTORY_INVALID")
    ordered: list[dict[str, Any]] = []
    prior: str | None = None
    while prior in by_prior:
        row = by_prior[prior]
        ordered.append(row)
        prior = row[event_key]
        if len(ordered) > len(rows):
            raise ChemicalPaperError("CHEMICAL_PAPER_HISTORY_INVALID")
    if len(ordered) != len(rows):
        raise ChemicalPaperError("CHEMICAL_PAPER_HISTORY_INVALID")
    return prior, ordered


def _validate_resolution_event(event: dict[str, Any]) -> None:
    metadata_keys = {"resolution_status", "confidence", "provenance", "gap_reason"}
    present = metadata_keys.intersection(event)
    if not present:
        return
    if event.get("field") != "resolved_smiles":
        raise ChemicalPaperError("CHEMICAL_PAPER_STATE_INVALID")
    try:
        normalized = _resolution_metadata(
            event.get("resolution_status"),
            event.get("confidence"),
            event.get("provenance"),
            event["actor"],
            event.get("gap_reason"),
        )
    except (KeyError, ChemicalPaperError) as exc:
        raise ChemicalPaperError("CHEMICAL_PAPER_STATE_INVALID") from exc
    if any(event.get(key) != value for key, value in normalized.items()):
        raise ChemicalPaperError("CHEMICAL_PAPER_STATE_INVALID")


def _validate_state(state: object) -> dict[str, Any]:
    if not isinstance(state, dict):
        raise ChemicalPaperError("CHEMICAL_PAPER_STATE_INVALID")
    try:
        schema = json.loads(STATE_SCHEMA.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ChemicalPaperError("CHEMICAL_PAPER_SCHEMA_INVALID") from exc
    if list(Draft202012Validator(schema).iter_errors(state)):
        raise ChemicalPaperError("CHEMICAL_PAPER_STATE_INVALID")
    imports = list(state["imports"].values())
    for key, row in state["imports"].items():
        if row["import_digest"] != key:
            raise ChemicalPaperError("CHEMICAL_PAPER_HISTORY_INVALID")
        if state["imports"].get(row["import_digest"]) != row:
            raise ChemicalPaperError("CHEMICAL_PAPER_HISTORY_INVALID")
        event = canonical_digest(
            {key: value for key, value in row.items() if key != "import_event_digest"}
        )
        if row["import_event_digest"] != event:
            raise ChemicalPaperError("CHEMICAL_PAPER_HISTORY_INVALID")
    import_head, _ = _validate_event_chain(
        imports, event_key="import_event_digest", prior_key="prior_import_event_digest"
    )
    current_import = state["imports"].get(state["current_import_digest"])
    if not isinstance(current_import, dict) or current_import["import_event_digest"] != import_head:
        raise ChemicalPaperError("CHEMICAL_PAPER_STATE_INVALID")
    correction_head, ordered_corrections = _validate_event_chain(
        state["field_corrections"], event_key="event_digest", prior_key="prior_event_digest"
    )
    review_head, ordered_reviews = _validate_event_chain(
        state["element_reviews"], event_key="event_digest", prior_key="prior_event_digest"
    )
    if state["field_correction_head_digest"] != correction_head or state["element_review_head_digest"] != review_head:
        raise ChemicalPaperError("CHEMICAL_PAPER_HISTORY_INVALID")
    page_counts = {row["import_digest"]: row["page_count"] for row in imports}
    corrections_by_digest = {
        event["event_digest"]: event for event in ordered_corrections
    }
    molecules_by_id: dict[str, dict[str, Any]] = {}
    for molecule in state["molecules"]:
        molecule_id = molecule["molecule_id"]
        if molecule_id in molecules_by_id:
            raise ChemicalPaperError("CHEMICAL_PAPER_STATE_INVALID")
        molecules_by_id[molecule_id] = molecule
        for field in ("resolved_smiles", *PROVENANCE_SMILES_FIELDS):
            candidate = molecule["fields"][field]["value"]
            try:
                _field(candidate)
            except (ChemicalPaperError, TypeError) as exc:
                raise ChemicalPaperError("CHEMICAL_PAPER_STATE_INVALID") from exc
            if field == "resolved_smiles" and candidate is not None and not _valid_resolved_smiles(candidate):
                raise ChemicalPaperError("CHEMICAL_PAPER_STATE_INVALID")

    correction_values: dict[tuple[str, str, str], str | None] = {}
    current_import_digest = state["current_import_digest"]
    current_molecule_pages = {
        molecule["page_index"] + 1 for molecule in state["molecules"]
    }
    for event in ordered_corrections:
        field = event["field"]
        try:
            _correction_value(
                field,
                event["value"],
                event.get("resolution_status"),
                event.get("gap_reason"),
            )
            if event["prior_value"] is not None:
                _correction_value(field, event["prior_value"])
            bound_import_digest = event["bound_import_digest"]
            page_count = page_counts[bound_import_digest]
            _correction_locator(event["pdf_locator"], page_count)
            _validate_resolution_event(event)
            key = (bound_import_digest, event["molecule_id"], field)
            if bound_import_digest == current_import_digest:
                molecule = molecules_by_id.get(event["molecule_id"])
                if molecule is None:
                    raise ChemicalPaperError("CHEMICAL_PAPER_STATE_INVALID")
                if event["bound_molecule_digest"] != molecule["molecule_digest"]:
                    raise ChemicalPaperError("CHEMICAL_PAPER_STATE_INVALID")
                if event["pdf_locator"]["page"] not in current_molecule_pages:
                    raise ChemicalPaperError("CHEMICAL_PAPER_STATE_INVALID")
                expected_prior = correction_values.get(
                    key, molecule["fields"][field]["value"]
                )
                if event["prior_value"] != expected_prior:
                    raise ChemicalPaperError("CHEMICAL_PAPER_STATE_INVALID")
            elif key in correction_values and event["prior_value"] != correction_values[key]:
                raise ChemicalPaperError("CHEMICAL_PAPER_STATE_INVALID")
            correction_values[key] = event["value"]
        except (KeyError, ChemicalPaperError) as exc:
            raise ChemicalPaperError("CHEMICAL_PAPER_STATE_INVALID") from exc
    for review in ordered_reviews:
        try:
            bound_import_digest = review["bound_import_digest"]
            if bound_import_digest not in page_counts:
                raise ChemicalPaperError("CHEMICAL_PAPER_STATE_INVALID")
            resolution_event_digest = review.get("bound_resolution_event_digest")
            if resolution_event_digest is not None:
                resolution_event = corrections_by_digest.get(resolution_event_digest)
                if (
                    resolution_event is None
                    or resolution_event["field"] != "resolved_smiles"
                    or resolution_event["molecule_id"] != review["molecule_id"]
                    or resolution_event["bound_import_digest"] != bound_import_digest
                    or resolution_event["bound_molecule_digest"]
                    != review["bound_molecule_digest"]
                ):
                    raise ChemicalPaperError("CHEMICAL_PAPER_STATE_INVALID")
            if bound_import_digest == current_import_digest:
                molecule = molecules_by_id.get(review["molecule_id"])
                if (
                    molecule is None
                    or review["bound_molecule_digest"]
                    != molecule["molecule_digest"]
                ):
                    raise ChemicalPaperError("CHEMICAL_PAPER_STATE_INVALID")
        except (KeyError, ChemicalPaperError) as exc:
            raise ChemicalPaperError("CHEMICAL_PAPER_STATE_INVALID") from exc
    if state.get("state_digest") != _canonical_state_digest(state):
        raise ChemicalPaperError("CHEMICAL_PAPER_STATE_INVALID")
    normalized = copy.deepcopy(state)
    normalized["field_corrections"] = copy.deepcopy(ordered_corrections)
    normalized["element_reviews"] = copy.deepcopy(ordered_reviews)
    return normalized


def load_chemical_paper_state(
    project: Path,
    study_id: str,
    *,
    source_index: ProjectSourceIndex | None = None,
    declared_studies: list[str] | None = None,
) -> dict[str, Any]:
    root = _project(project)
    path = _state_path(root, study_id)
    if not path.is_file() or path.is_symlink():
        raise ChemicalPaperError("CHEMICAL_PAPER_NOT_IMPORTED")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ChemicalPaperError("CHEMICAL_PAPER_STATE_INVALID") from exc
    state = _validate_state(value)
    try:
        index = source_index or build_project_source_index(root)
        studies = declared_studies or declared_study_ids(root)
        if index.project != root or any(
            current_study_id not in index.bundles_by_study
            for current_study_id in studies
        ):
            raise SourceTruthError("SOURCE_TRUTH_IDENTITY_MISMATCH")
        bundle = index.bundles_by_study.get(study_id)
        if bundle is None:
            raise SourceTruthError("SOURCE_TRUTH_MISSING")
        resolved_study_id, resolved_source = project_source_binding(
            root,
            state["source_id"],
            source_index=index,
        )
    except SourceTruthError as exc:
        raise ChemicalPaperError("CHEMICAL_PAPER_SOURCE_TRUTH_STALE") from exc
    sources = bundle.get("sources")
    current = [
        row
        for row in sources
        if isinstance(row, dict)
        and row.get("document_role") == "MAIN"
        and row.get("source_id") == state["source_id"]
        and isinstance(row.get("pdf"), dict)
        and row["pdf"].get("sha256") == state["source_pdf_sha256"]
    ] if isinstance(sources, list) else []
    active = state["imports"][state["current_import_digest"]]
    if (
        bundle.get("bundle_digest") != state["source_truth_bundle_digest"]
        or state["project_id"] != root.name
        or state["study_id"] != study_id
        or bundle.get("project_id") != root.name
        or bundle.get("study_id") != study_id
        or resolved_study_id != study_id
        or len(current) != 1
        or resolved_source != current[0]
        or active["source_id"] != state["source_id"]
        or active["source_pdf_sha256"] != state["source_pdf_sha256"]
        or active["source_truth_bundle_digest"] != state["source_truth_bundle_digest"]
        or active["page_count"] != current[0]["page_count"]
        or active["molecule_count"] != len(state["molecules"])
        or any(
            molecule["page_index"] >= current[0]["page_count"]
            for molecule in state["molecules"]
        )
    ):
        raise ChemicalPaperError("CHEMICAL_PAPER_SOURCE_TRUTH_STALE")
    try:
        source_truth_asset(
            root,
            study_id,
            state["source_id"],
            "pdf",
            source_index=index,
        )
    except SourceTruthError as exc:
        raise ChemicalPaperError(exc.code) from exc
    return state


def chemical_paper_current_binding(project: Path, study_id: str) -> dict[str, str]:
    """Return current Chemical provenance without exposing archive or molecule payloads."""
    state = load_chemical_paper_state(project, study_id)
    return {
        "source_pdf_sha256": state["source_pdf_sha256"],
        "source_truth_bundle_digest": state["source_truth_bundle_digest"],
        "import_digest": state["current_import_digest"],
        "state_digest": state["state_digest"],
    }


def _safe_member_name(name: str) -> str:
    normalized = unicodedata.normalize("NFC", name)
    pure = PurePosixPath(normalized)
    if (
        not normalized
        or "\x00" in normalized
        or "\\" in normalized
        or normalized.startswith(("/", "\\"))
        or re.match(r"^[A-Za-z]:", normalized)
        or any(part in {"", ".", ".."} for part in pure.parts)
        or normalized.endswith("/")
    ):
        raise ChemicalPaperError("ZIP_PATH_UNSAFE")
    return normalized


def _read_archive_snapshot(path: Path) -> bytes:
    path = Path(path)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        path_metadata = os.lstat(path)
        if not stat.S_ISREG(path_metadata.st_mode):
            raise ChemicalPaperError("ZIP_INVALID")
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_dev != path_metadata.st_dev
            or metadata.st_ino != path_metadata.st_ino
        ):
            raise ChemicalPaperError("ZIP_INVALID")
        if metadata.st_size > MAX_ARCHIVE_BYTES:
            raise ChemicalPaperError("ZIP_SIZE_LIMIT")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            snapshot = handle.read(MAX_ARCHIVE_BYTES + 1)
    except ChemicalPaperError:
        raise
    except OSError as exc:
        raise ChemicalPaperError("ZIP_INVALID") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if len(snapshot) > MAX_ARCHIVE_BYTES:
        raise ChemicalPaperError("ZIP_SIZE_LIMIT")
    return snapshot


def _zip_inventory(source: Path | bytes) -> tuple[zipfile.ZipFile, list[zipfile.ZipInfo]]:
    snapshot = source if isinstance(source, bytes) else _read_archive_snapshot(source)
    try:
        archive = zipfile.ZipFile(io.BytesIO(snapshot), "r")
        members = archive.infolist()
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise ChemicalPaperError("ZIP_INVALID") from exc
    if not members or len(members) > MAX_ENTRY_COUNT:
        archive.close()
        raise ChemicalPaperError("ZIP_ENTRY_COUNT_LIMIT")
    names: set[str] = set()
    total = 0
    try:
        for member in members:
            name = _safe_member_name(member.filename)
            folded = name.casefold()
            if folded in names:
                raise ChemicalPaperError("ZIP_DUPLICATE_ENTRY")
            names.add(folded)
            mode = (member.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode):
                raise ChemicalPaperError("ZIP_SYMLINK_UNSAFE")
            if member.flag_bits & 0x1:
                raise ChemicalPaperError("ZIP_ENCRYPTED")
            if PurePosixPath(name).suffix.casefold() in NESTED_ARCHIVE_SUFFIXES:
                raise ChemicalPaperError("ZIP_NESTED_ARCHIVE")
            if member.file_size > MAX_ENTRY_BYTES:
                raise ChemicalPaperError("ZIP_SIZE_LIMIT")
            total += member.file_size
            if total > MAX_TOTAL_BYTES:
                raise ChemicalPaperError("ZIP_SIZE_LIMIT")
            ratio = member.file_size / max(member.compress_size, 1)
            if ratio > MAX_COMPRESSION_RATIO:
                raise ChemicalPaperError("ZIP_COMPRESSION_RATIO_LIMIT")
    except Exception:
        archive.close()
        raise
    return archive, members


def _read_entry(archive: zipfile.ZipFile, member: zipfile.ZipInfo) -> bytes:
    try:
        payload = archive.read(member)
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise ChemicalPaperError("ZIP_ENTRY_INVALID") from exc
    if len(payload) != member.file_size:
        raise ChemicalPaperError("ZIP_ENTRY_INVALID")
    return payload


def _json_entry(payload: bytes) -> object:
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ChemicalPaperError("CHEMICAL_PAPER_JSON_INVALID") from exc


def _bbox(value: object) -> list[float]:
    if (
        not isinstance(value, list)
        or len(value) != 4
        or any(not isinstance(item, (int, float)) or isinstance(item, bool) for item in value)
    ):
        raise ChemicalPaperError("MOLECULE_BBOX_INVALID")
    result = [float(item) for item in value]
    if any(item < 0 or item > 1 for item in result) or result[0] > result[2] or result[1] > result[3]:
        raise ChemicalPaperError("MOLECULE_BBOX_INVALID")
    return result


def _molblock_elements(value: object) -> tuple[str, dict[str, int]]:
    if not isinstance(value, str) or not value.endswith(("M  END", "M  END\n", "M  END\r\n")):
        raise ChemicalPaperError("MOLBLOCK_INVALID")
    lines = value.replace("\r\n", "\n").splitlines()
    counts_index = next((index for index, line in enumerate(lines) if "V2000" in line or "V3000" in line), None)
    if counts_index is None:
        raise ChemicalPaperError("MOLBLOCK_INVALID")
    counts: Counter[str] = Counter()
    if "V2000" in lines[counts_index]:
        try:
            atom_count = int(lines[counts_index][:3])
        except ValueError as exc:
            raise ChemicalPaperError("MOLBLOCK_INVALID") from exc
        atom_lines = lines[counts_index + 1 : counts_index + 1 + atom_count]
        if atom_count < 0 or len(atom_lines) != atom_count:
            raise ChemicalPaperError("MOLBLOCK_INVALID")
        for line in atom_lines:
            parts = line.split()
            if len(parts) < 4:
                raise ChemicalPaperError("MOLBLOCK_INVALID")
            symbol = parts[3]
            if not _ELEMENT.fullmatch(symbol) or symbol not in _ELEMENTS:
                raise ChemicalPaperError("MOLBLOCK_INVALID")
            counts[symbol] += 1
        version = "V2000"
    else:
        try:
            begin = lines.index("M  V30 BEGIN ATOM")
            end = lines.index("M  V30 END ATOM")
        except ValueError as exc:
            raise ChemicalPaperError("MOLBLOCK_INVALID") from exc
        if begin >= end or "M  V30 BEGIN CTAB" not in lines or "M  V30 END CTAB" not in lines:
            raise ChemicalPaperError("MOLBLOCK_INVALID")
        for line in lines[begin + 1 : end]:
            parts = line.split()
            if len(parts) < 7 or parts[:2] != ["M", "V30"]:
                raise ChemicalPaperError("MOLBLOCK_INVALID")
            symbol = parts[3]
            if not _ELEMENT.fullmatch(symbol) or symbol not in _ELEMENTS:
                raise ChemicalPaperError("MOLBLOCK_INVALID")
            counts[symbol] += 1
        if not counts:
            raise ChemicalPaperError("MOLBLOCK_INVALID")
        version = "V3000"
    return version, dict(sorted(counts.items()))


def _field(value: object) -> dict[str, Any]:
    if value is None or value == "":
        return {"status": "unresolved", "value": None}
    if not isinstance(value, str) or value != value.strip() or len(value) > 20000:
        raise ChemicalPaperError("MOLECULE_FIELD_INVALID")
    return {"status": "candidate", "value": value}


def _normalize_molecules(value: object, page_count: int, import_digest_seed: str) -> list[dict[str, Any]]:
    if not isinstance(value, dict) or set(value) != {"molecules"} or not isinstance(value["molecules"], list) or not value["molecules"]:
        raise ChemicalPaperError("CHEMICAL_PAPER_CONTRACT_MISSING")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    required = {
        "mol_id", "page_idx", "bbox_normalized", "mol_idt", "mol_block",
        *PROVENANCE_SMILES_FIELDS,
    }
    for raw in value["molecules"]:
        if not isinstance(raw, dict) or not required <= set(raw):
            raise ChemicalPaperError("MOLECULE_INVALID")
        molecule_id = _identifier(raw.get("mol_id"), "MOLECULE_ID_INVALID")
        if molecule_id in seen:
            raise ChemicalPaperError("MOLECULE_ID_DUPLICATE")
        seen.add(molecule_id)
        page = raw.get("page_idx")
        if not isinstance(page, int) or isinstance(page, bool) or page < 0 or page >= page_count:
            raise ChemicalPaperError("MOLECULE_PAGE_INVALID")
        block_version, elements = _molblock_elements(raw.get("mol_block"))
        expanded = _field(raw.get("smiles_expanded"))
        unexpanded = _field(raw.get("smiles_unexpanded"))
        resolved, _ = _first_valid_smiles_candidate(
            expanded["value"],
            unexpanded["value"],
        )
        body = {
            "molecule_id": molecule_id,
            "page_index": page,
            "normalized_bbox": _bbox(raw.get("bbox_normalized")),
            "mol_block": raw["mol_block"],
            "molblock_format": block_version,
            "fields": {
                "mol_idt": _field(raw.get("mol_idt")),
                "resolved_smiles": _field(resolved),
                "smiles_expanded": expanded,
                "smiles_unexpanded": unexpanded,
            },
            "element_candidate_counts": elements,
            "element_review_state": "not_reviewed",
            "bound_import_seed": import_digest_seed,
        }
        body["molecule_digest"] = canonical_digest(body)
        rows.append(body)
    return rows


def _source_binding(project: Path, study_id: str, pdf_sha256: str) -> tuple[dict[str, Any], dict[str, Any]]:
    _digest(pdf_sha256, "SOURCE_PDF_SHA256_INVALID")
    try:
        bundle = load_source_truth_bundle(project, study_id)
        if study_id not in declared_study_ids(project):
            raise SourceTruthError("STUDY_NOT_FOUND")
    except SourceTruthError as exc:
        raise ChemicalPaperError(exc.code) from exc
    if bundle.get("project_id") != project.name or bundle.get("study_id") != study_id:
        raise ChemicalPaperError("SOURCE_TRUTH_IDENTITY_MISMATCH")
    sources = bundle.get("sources")
    matches = [
        row for row in sources if isinstance(row, dict) and row.get("document_role") == "MAIN"
        and isinstance(row.get("pdf"), dict) and row["pdf"].get("sha256") == pdf_sha256
    ] if isinstance(sources, list) else []
    if len(matches) != 1:
        raise ChemicalPaperError("SOURCE_PDF_STALE")
    try:
        resolved_study_id, resolved_source = project_source_binding(
            project,
            matches[0]["source_id"],
        )
    except SourceTruthError as exc:
        raise ChemicalPaperError(exc.code) from exc
    if resolved_study_id != study_id or resolved_source != matches[0]:
        raise ChemicalPaperError("SOURCE_BINDING_INVALID")
    return bundle, matches[0]


def _validate_main(value: object, expected_pages: int) -> tuple[str, str, int]:
    if not isinstance(value, dict):
        raise ChemicalPaperError("CHEMICAL_PAPER_MAIN_INVALID")
    backend, version, pages = value.get("_backend"), value.get("_version_name"), value.get("pdf_info")
    if not isinstance(backend, str) or not backend or not isinstance(version, str) or not version or not isinstance(pages, list):
        raise ChemicalPaperError("CHEMICAL_PAPER_MAIN_INVALID")
    if len(pages) != expected_pages:
        raise ChemicalPaperError("CHEMICAL_PAPER_PAGE_COUNT_MISMATCH")
    indexes: list[int] = []
    for page in pages:
        if not isinstance(page, dict) or not isinstance(page.get("page_idx"), int) or isinstance(page.get("page_idx"), bool):
            raise ChemicalPaperError("CHEMICAL_PAPER_PAGE_INVALID")
        indexes.append(page["page_idx"])
    if sorted(indexes) != list(range(expected_pages)):
        raise ChemicalPaperError("CHEMICAL_PAPER_PAGE_INVALID")
    return backend, version, len(pages)


def _archive_payload(path: Path, expected_pages: int) -> dict[str, Any]:
    snapshot = _read_archive_snapshot(path)
    archive, members = _zip_inventory(snapshot)
    try:
        if len(members) != 3:
            raise ChemicalPaperError("CHEMICAL_PAPER_FILE_SET_INVALID")
        decoded: list[tuple[zipfile.ZipInfo, bytes]] = [(member, _read_entry(archive, member)) for member in members]
    finally:
        archive.close()
    markdown = [(member, payload) for member, payload in decoded if member.filename.casefold().endswith(".md")]
    json_rows = [(member, payload, _json_entry(payload)) for member, payload in decoded if member.filename.casefold().endswith(".json")]
    if len(markdown) != 1 or len(json_rows) != 2:
        raise ChemicalPaperError("CHEMICAL_PAPER_FILE_SET_INVALID")
    main = [row for row in json_rows if isinstance(row[2], dict) and {"_backend", "_version_name", "pdf_info"} <= set(row[2])]
    molecules = [row for row in json_rows if isinstance(row[2], dict) and "molecules" in row[2]]
    if len(main) != 1 or len(molecules) != 1:
        raise ChemicalPaperError("CHEMICAL_PAPER_CONTRACT_MISSING")
    backend, version, page_count = _validate_main(main[0][2], expected_pages)
    inventory = []
    for member, payload in [(row[0], row[1]) for row in json_rows] + markdown:
        kind = (
            "main_layout_json" if member is main[0][0]
            else "molecule_info_json" if member is molecules[0][0]
            else "markdown"
        )
        inventory.append({"entry_name": member.filename, "file_kind": kind, "sha256": hashlib.sha256(payload).hexdigest(), "size_bytes": len(payload)})
    inventory.sort(key=lambda row: row["file_kind"])
    archive_sha = hashlib.sha256(snapshot).hexdigest()
    seed = canonical_digest({"archive_sha256": archive_sha, "inventory": inventory})
    normalized_molecules = _normalize_molecules(molecules[0][2], page_count, seed)
    return {
        "archive_sha256": archive_sha,
        "backend": backend,
        "version": version,
        "page_count": page_count,
        "entry_inventory": inventory,
        "molecules": normalized_molecules,
    }


def import_chemical_paper(
    project: Path,
    study_id: str,
    source_pdf_sha256: str,
    zip_path: Path,
    actor: object,
) -> dict[str, Any]:
    """Validate and atomically bind one explicit study/PDF/archive tuple."""
    root = _project(project)
    study_id = _identifier(study_id, "STUDY_ID_INVALID")
    who = _actor(actor)
    try:
        receipt_digest = acquisition_receipt_digest(root)
    except SourceTruthError as exc:
        raise ChemicalPaperError(exc.code) from exc
    bundle, source = _source_binding(root, study_id, source_pdf_sha256)
    try:
        source_truth_asset(root, study_id, source["source_id"], "pdf")
    except SourceTruthError as exc:
        raise ChemicalPaperError(exc.code) from exc
    parsed = _archive_payload(Path(zip_path), int(source["page_count"]))
    import_body = {
        "archive_sha256": parsed["archive_sha256"],
        "source_pdf_sha256": source_pdf_sha256,
        "source_truth_bundle_digest": bundle["bundle_digest"],
        "source_id": source["source_id"],
        "backend": parsed["backend"],
        "version": parsed["version"],
        "page_count": parsed["page_count"],
        "entry_inventory": parsed["entry_inventory"],
        "molecule_count": len(parsed["molecules"]),
        "reaction_data_status": "unavailable_not_provided",
    }
    import_digest = canonical_digest(import_body)
    path = _state_path(root, study_id)
    try:
        with project_write_lock(root):
            try:
                current_receipt_digest = acquisition_receipt_digest(root)
                current_bundle, current_source = _source_binding(
                    root,
                    study_id,
                    source_pdf_sha256,
                )
            except (SourceTruthError, ChemicalPaperError) as exc:
                raise ChemicalPaperError("SOURCE_INPUT_STALE") from exc
            if (
                current_receipt_digest != receipt_digest
                or current_bundle.get("bundle_digest") != bundle.get("bundle_digest")
                or current_source != source
            ):
                raise ChemicalPaperError("SOURCE_INPUT_STALE")
            try:
                source_truth_asset(root, study_id, source["source_id"], "pdf")
            except SourceTruthError as exc:
                raise ChemicalPaperError(exc.code) from exc
            try:
                current_archive = _read_archive_snapshot(Path(zip_path))
            except ChemicalPaperError as exc:
                raise ChemicalPaperError("ZIP_INPUT_STALE") from exc
            if hashlib.sha256(current_archive).hexdigest() != parsed["archive_sha256"]:
                raise ChemicalPaperError("ZIP_INPUT_STALE")
            existing: dict[str, Any] | None = None
            if path.exists():
                existing = load_chemical_paper_state(root, study_id)
                active = existing["imports"][existing["current_import_digest"]]
                if active["archive_sha256"] == parsed["archive_sha256"] and active["source_pdf_sha256"] == source_pdf_sha256:
                    return {"status": "unchanged", "study_id": study_id, "version_token": _version_token(existing)}
            prior_import = (
                existing["imports"][existing["current_import_digest"]]["import_event_digest"]
                if existing else None
            )
            event = {
                **import_body,
                "import_digest": import_digest,
                "imported_at": _now(),
                "actor": who,
                "prior_import_event_digest": prior_import,
            }
            event["import_event_digest"] = canonical_digest(event)
            state: dict[str, Any] = {
                "schema_version": "chemical-paper-state.v2",
                "project_id": root.name,
                "study_id": study_id,
                "source_id": source["source_id"],
                "source_truth_bundle_digest": bundle["bundle_digest"],
                "source_pdf_sha256": source_pdf_sha256,
                "current_import_digest": import_digest,
                "imports": {**(existing["imports"] if existing else {}), import_digest: event},
                "molecules": parsed["molecules"],
                "field_corrections": existing["field_corrections"] if existing else [],
                "field_correction_head_digest": existing["field_correction_head_digest"] if existing else None,
                "element_reviews": existing["element_reviews"] if existing else [],
                "element_review_head_digest": existing["element_review_head_digest"] if existing else None,
            }
            state["state_digest"] = _canonical_state_digest(state)
            _validate_state(state)
            _atomic_json(path, state)
    except PaperEvidenceStoreError as exc:
        raise ChemicalPaperError(exc.code) from exc
    return {"status": "imported", "study_id": study_id, "version_token": _version_token(state)}


def _molecule(state: dict[str, Any], molecule_id: str) -> dict[str, Any]:
    molecule_id = _identifier(molecule_id, "MOLECULE_ID_INVALID")
    matches = [row for row in state["molecules"] if row["molecule_id"] == molecule_id]
    if len(matches) != 1:
        raise ChemicalPaperError("MOLECULE_NOT_FOUND")
    return matches[0]


def _molecule_by_index(state: dict[str, Any], molecule_index: object) -> dict[str, Any]:
    if (
        not isinstance(molecule_index, int)
        or isinstance(molecule_index, bool)
        or molecule_index < 0
        or molecule_index >= len(state["molecules"])
    ):
        raise ChemicalPaperError("MOLECULE_NOT_FOUND")
    return state["molecules"][molecule_index]


def _current_value(state: dict[str, Any], molecule: dict[str, Any], field: str) -> str | None:
    value = molecule["fields"][field]["value"]
    for event in state["field_corrections"]:
        if event["bound_import_digest"] == state["current_import_digest"] and event["molecule_id"] == molecule["molecule_id"] and event["field"] == field:
            value = event["value"]
    return value


def _current_resolved_smiles_event(
    state: dict[str, Any], molecule: dict[str, Any]
) -> dict[str, Any] | None:
    current: dict[str, Any] | None = None
    for event in state["field_corrections"]:
        if (
            event["bound_import_digest"] == state["current_import_digest"]
            and event["molecule_id"] == molecule["molecule_id"]
            and event["field"] == "resolved_smiles"
        ):
            current = event
    return current


def _resolved_smiles_resolution(
    state: dict[str, Any], molecule: dict[str, Any]
) -> dict[str, Any]:
    """Return the honest, read-only resolution classification for one molecule.

    Imported candidate values are classified only in this projection.  The
    authoritative state and append-only correction history are not rewritten.
    """

    value = _current_value(state, molecule, "resolved_smiles")
    event = _current_resolved_smiles_event(state, molecule)
    if event is not None and event.get("resolution_status") == "AI_PROVISIONAL":
        return {
            "resolved_smiles_status": "AI_PROVISIONAL",
            "resolved_smiles": value,
            "confidence": event["confidence"],
            "provenance": _researcher_safe_provenance(event["provenance"]),
            "pdf_locator": copy.deepcopy(event["pdf_locator"]),
            "gap_reason": None,
            "legacy_unclassified": False,
        }
    if event is not None and event.get("resolution_status") == "BLOCKED":
        return {
            "resolved_smiles_status": "BLOCKED",
            "resolved_smiles": None,
            "confidence": None,
            "provenance": None,
            "pdf_locator": copy.deepcopy(event["pdf_locator"]),
            "gap_reason": event["gap_reason"],
            "legacy_unclassified": False,
        }
    if event is not None and event.get("resolution_status") == "CONFIRMED":
        return {
            "resolved_smiles_status": "CONFIRMED",
            "resolved_smiles": value,
            "confidence": None,
            "provenance": {"kind": "researcher_confirmation"},
            "pdf_locator": copy.deepcopy(event["pdf_locator"]),
            "gap_reason": None,
            "legacy_unclassified": False,
        }
    if value is None:
        return {
            "resolved_smiles_status": "BLOCKED",
            "resolved_smiles": None,
            "confidence": None,
            "provenance": None,
            "pdf_locator": {
                "page": molecule["page_index"] + 1,
                "bbox": copy.deepcopy(molecule["normalized_bbox"]),
            },
            "gap_reason": "resolved_smiles_not_available",
            "legacy_unclassified": False,
        }
    expanded = molecule["fields"]["smiles_expanded"]["value"]
    unexpanded = molecule["fields"]["smiles_unexpanded"]["value"]
    candidate, selected_source = _first_valid_smiles_candidate(expanded, unexpanded)
    if candidate == value and selected_source is not None and event is None:
        return {
            "resolved_smiles_status": "AI_PROVISIONAL",
            "resolved_smiles": value,
            # Existing imports do not carry a confidence score.  Zero is an
            # explicit conservative value, not a claim of researcher review.
            "confidence": 0.0,
            "provenance": {
                "kind": "legacy_candidate_projection",
                "source": "chemical_paper_import",
                "source_field": selected_source,
            },
            "pdf_locator": {
                "page": molecule["page_index"] + 1,
                "bbox": copy.deepcopy(molecule["normalized_bbox"]),
            },
            "gap_reason": None,
            "legacy_unclassified": False,
        }
    return {
        "resolved_smiles_status": "BLOCKED",
        "resolved_smiles": None,
        "confidence": None,
        "provenance": None,
        "pdf_locator": (
            copy.deepcopy(event["pdf_locator"])
            if event is not None
            else {
                "page": molecule["page_index"] + 1,
                "bbox": copy.deepcopy(molecule["normalized_bbox"]),
            }
        ),
        "gap_reason": "legacy_resolution_status_missing",
        "legacy_unclassified": True,
        "legacy_value_present": True,
    }


def _resolved_smiles_details(
    state: dict[str, Any], molecule: dict[str, Any]
) -> tuple[str | None, dict[str, str | bool | None]]:
    expanded = molecule["fields"]["smiles_expanded"]["value"]
    unexpanded = molecule["fields"]["smiles_unexpanded"]["value"]
    corrected = any(
        event["bound_import_digest"] == state["current_import_digest"]
        and event["molecule_id"] == molecule["molecule_id"]
        and event["field"] == "resolved_smiles"
        for event in state["field_corrections"]
    )
    selected_source: str | None
    if corrected:
        selected_source = "researcher_correction"
    else:
        _, selected_source = _first_valid_smiles_candidate(expanded, unexpanded)
    return _current_value(state, molecule, "resolved_smiles"), {
        "expanded": expanded,
        "unexpanded": unexpanded,
        "selected_source": selected_source,
        "candidate_difference": (
            expanded is not None
            and unexpanded is not None
            and expanded != unexpanded
        ),
    }


def _current_element_review(state: dict[str, Any], molecule: dict[str, Any]) -> tuple[str, dict[str, int], str | None]:
    review_state = "not_reviewed"
    counts = molecule["element_candidate_counts"]
    digest: str | None = None
    event = _current_element_review_event(state, molecule)
    if event is not None:
        review_state = event["state"]
        counts = event["reviewed_counts"] if event["reviewed_counts"] is not None else counts
        digest = event["event_digest"]
    return review_state, counts, digest


def _current_element_review_event(
    state: dict[str, Any], molecule: dict[str, Any]
) -> dict[str, Any] | None:
    current: dict[str, Any] | None = None
    for event in state["element_reviews"]:
        if (
            event["bound_import_digest"] == state["current_import_digest"]
            and event["molecule_id"] == molecule["molecule_id"]
        ):
            current = event
    return current


def _actor_identity(value: object) -> tuple[str, str] | None:
    if not isinstance(value, dict):
        return None
    actor_type = value.get("actor_type")
    actor_label = value.get("actor_label")
    if not isinstance(actor_type, str) or not isinstance(actor_label, str):
        return None
    return actor_type, actor_label


def _source_locator_binding(
    project_root: Path,
    project_device: int,
    project_inode: int,
    project_id: str,
    study_id: str,
    source_id: str,
    bundle_digest: str,
    pdf_sha256: str,
) -> str:
    digest = canonical_digest(
        {
            "project_instance_root": os.fspath(project_root),
            "project_instance_device": project_device,
            "project_instance_inode": project_inode,
            "project_id": project_id,
            "study_id": study_id,
            "source_id": source_id,
            "source_truth_bundle_digest": bundle_digest,
            "source_pdf_sha256": pdf_sha256,
        }
    )
    encoded = base64.urlsafe_b64encode(bytes.fromhex(digest)).decode("ascii").rstrip("=")
    return f"cpb1.{encoded}"


def resolve_chemical_paper_pdf_locator(
    project: Path,
    source_id: str,
    binding: str,
) -> ChemicalPaperPdfLocatorDescriptor:
    """Resolve one exact Chemical PDF binding with a fresh whole-project index."""

    root = _project(project)
    project_device, project_inode = _project_instance_identity(root)
    source_id = _identifier(source_id, "SOURCE_ID_INVALID")
    if not isinstance(binding, str) or not re.fullmatch(r"cpb1\.[A-Za-z0-9_-]{43}", binding):
        raise ChemicalPaperError("CHEMICAL_PAPER_LOCATOR_INVALID")
    try:
        index = build_project_source_index(root)
        study_id, source = project_source_binding(
            root,
            source_id,
            source_index=index,
        )
        bundle = index.bundles_by_study[study_id]
    except (KeyError, SourceTruthError) as exc:
        raise ChemicalPaperError("STALE_CHEMICAL_PAPER_LOCATOR") from exc
    pdf = source.get("pdf")
    if (
        source.get("document_role") != "MAIN"
        or not isinstance(pdf, dict)
        or not isinstance(pdf.get("sha256"), str)
        or not _SHA256.fullmatch(pdf["sha256"])
        or not isinstance(pdf.get("size_bytes"), int)
        or isinstance(pdf["size_bytes"], bool)
        or pdf["size_bytes"] < 1
    ):
        raise ChemicalPaperError("STALE_CHEMICAL_PAPER_LOCATOR")
    bundle_digest = bundle.get("bundle_digest")
    if not isinstance(bundle_digest, str) or not _SHA256.fullmatch(bundle_digest):
        raise ChemicalPaperError("STALE_CHEMICAL_PAPER_LOCATOR")
    expected = _source_locator_binding(
        root,
        project_device,
        project_inode,
        root.name,
        study_id,
        source_id,
        bundle_digest,
        pdf["sha256"],
    )
    if not secrets.compare_digest(binding, expected):
        raise ChemicalPaperError("STALE_CHEMICAL_PAPER_LOCATOR")
    page_count = source.get("page_count")
    if not isinstance(page_count, int) or isinstance(page_count, bool) or page_count < 1:
        raise ChemicalPaperError("STALE_CHEMICAL_PAPER_LOCATOR")
    try:
        asset = source_truth_asset(
            root,
            study_id,
            source_id,
            "pdf",
            source_index=index,
        )
    except SourceTruthError as exc:
        raise ChemicalPaperError(exc.code) from exc
    if _project_instance_identity(root) != (project_device, project_inode):
        raise ChemicalPaperError("STALE_CHEMICAL_PAPER_LOCATOR")
    return ChemicalPaperPdfLocatorDescriptor(
        project_root=root,
        project_device=project_device,
        project_inode=project_inode,
        study_id=study_id,
        source_id=source_id,
        binding=binding,
        source_truth_bundle_digest=bundle_digest,
        pdf_sha256=pdf["sha256"],
        pdf_size_bytes=pdf["size_bytes"],
        asset_path=asset,
        page_count=page_count,
    )


def verify_chemical_paper_pdf_locator(
    descriptor: ChemicalPaperPdfLocatorDescriptor,
) -> None:
    """Rebuild current authority after snapshotting and reject any binding drift."""

    if not isinstance(descriptor, ChemicalPaperPdfLocatorDescriptor):
        raise ChemicalPaperError("CHEMICAL_PAPER_LOCATOR_INVALID")
    current = resolve_chemical_paper_pdf_locator(
        descriptor.project_root,
        descriptor.source_id,
        descriptor.binding,
    )
    if current != descriptor:
        raise ChemicalPaperError("STALE_CHEMICAL_PAPER_LOCATOR")


def _verify_private_pdf_snapshot_file(
    path: object,
    expected_sha256: str,
    expected_size_bytes: int,
) -> None:
    try:
        snapshot_path = Path(path)
    except TypeError as exc:
        raise ChemicalPaperError("CHEMICAL_PAPER_PDF_SNAPSHOT_INVALID") from exc
    if not snapshot_path.is_absolute():
        raise ChemicalPaperError("CHEMICAL_PAPER_PDF_SNAPSHOT_INVALID")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor: int | None = None
    try:
        path_metadata = os.lstat(snapshot_path)
        if (
            not stat.S_ISREG(path_metadata.st_mode)
            or stat.S_IMODE(path_metadata.st_mode) & 0o077
        ):
            raise ChemicalPaperError("CHEMICAL_PAPER_PDF_SNAPSHOT_INVALID")
        descriptor = os.open(snapshot_path, flags)
        opened_metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened_metadata.st_mode)
            or stat.S_IMODE(opened_metadata.st_mode) & 0o077
            or opened_metadata.st_dev != path_metadata.st_dev
            or opened_metadata.st_ino != path_metadata.st_ino
        ):
            raise ChemicalPaperError("CHEMICAL_PAPER_PDF_SNAPSHOT_INVALID")
        if hasattr(os, "geteuid") and opened_metadata.st_uid != os.geteuid():
            raise ChemicalPaperError("CHEMICAL_PAPER_PDF_SNAPSHOT_INVALID")
        if opened_metadata.st_size != expected_size_bytes:
            raise ChemicalPaperError("STALE_CHEMICAL_PAPER_LOCATOR")
        digest = hashlib.sha256()
        observed_size = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            observed_size += len(chunk)
            if observed_size > expected_size_bytes:
                raise ChemicalPaperError("STALE_CHEMICAL_PAPER_LOCATOR")
            digest.update(chunk)
        final_metadata = os.fstat(descriptor)
        if (
            final_metadata.st_dev != opened_metadata.st_dev
            or final_metadata.st_ino != opened_metadata.st_ino
            or final_metadata.st_size != opened_metadata.st_size
            or observed_size != expected_size_bytes
            or not secrets.compare_digest(digest.hexdigest(), expected_sha256)
        ):
            raise ChemicalPaperError("STALE_CHEMICAL_PAPER_LOCATOR")
    except ChemicalPaperError:
        raise
    except OSError as exc:
        raise ChemicalPaperError("CHEMICAL_PAPER_PDF_SNAPSHOT_INVALID") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def verify_chemical_paper_pdf_snapshot(
    descriptor: ChemicalPaperPdfLocatorDescriptor,
    snapshot: object | None = None,
    *,
    sha256: str | None = None,
    size_bytes: int | None = None,
) -> None:
    """Bind immutable snapshot bytes, then reverify current Chemical authority.

    Object mode verifies private snapshot provenance and bytes for Release render
    paths.  Explicit ``sha256``/``size_bytes`` mode is an internal composition
    hook that proves only caller-bound bytes; Dashboard render paths must not use
    it as snapshot provenance.
    """

    if not isinstance(descriptor, ChemicalPaperPdfLocatorDescriptor):
        raise ChemicalPaperError("CHEMICAL_PAPER_LOCATOR_INVALID")
    if snapshot is None:
        if (
            not isinstance(sha256, str)
            or not _SHA256.fullmatch(sha256)
            or not isinstance(size_bytes, int)
            or isinstance(size_bytes, bool)
            or size_bytes < 1
        ):
            raise ChemicalPaperError("CHEMICAL_PAPER_PDF_SNAPSHOT_INVALID")
        observed_sha256 = sha256
        observed_size_bytes = size_bytes
        snapshot_path: object | None = None
    else:
        if sha256 is not None or size_bytes is not None:
            raise ChemicalPaperError("CHEMICAL_PAPER_PDF_SNAPSHOT_INVALID")
        try:
            snapshot_project_root = Path(
                getattr(snapshot, "project_instance_root")
            )
            snapshot_identity = tuple(
                getattr(snapshot, field)
                for field in (
                    "project_id",
                    "project_device",
                    "project_inode",
                    "study_id",
                    "source_id",
                    "kind",
                    "bundle_digest",
                    "page_count",
                )
            )
            observed_sha256 = getattr(snapshot, "sha256")
            observed_size_bytes = getattr(snapshot, "size_bytes")
            snapshot_path = getattr(snapshot, "path")
        except (AttributeError, TypeError) as exc:
            raise ChemicalPaperError("CHEMICAL_PAPER_PDF_SNAPSHOT_INVALID") from exc
        expected_identity = (
            descriptor.project_root.name,
            descriptor.project_device,
            descriptor.project_inode,
            descriptor.study_id,
            descriptor.source_id,
            "pdf",
            descriptor.source_truth_bundle_digest,
            descriptor.page_count,
        )
        if (
            snapshot_project_root != descriptor.project_root
            or snapshot_identity != expected_identity
        ):
            raise ChemicalPaperError("STALE_CHEMICAL_PAPER_LOCATOR")
    if (
        not isinstance(observed_sha256, str)
        or not secrets.compare_digest(observed_sha256, descriptor.pdf_sha256)
        or not isinstance(observed_size_bytes, int)
        or isinstance(observed_size_bytes, bool)
        or observed_size_bytes != descriptor.pdf_size_bytes
    ):
        raise ChemicalPaperError("STALE_CHEMICAL_PAPER_LOCATOR")
    if snapshot_path is not None:
        _verify_private_pdf_snapshot_file(
            snapshot_path,
            descriptor.pdf_sha256,
            descriptor.pdf_size_bytes,
        )
    verify_chemical_paper_pdf_locator(descriptor)


def chemical_paper_pdf_locator(
    project: Path,
    source_id: str,
    binding: str,
) -> tuple[Path, int]:
    """Backward-compatible tuple view of the verified Chemical locator."""

    descriptor = resolve_chemical_paper_pdf_locator(project, source_id, binding)
    return descriptor.asset_path, descriptor.page_count


def _require_mutation_binding(
    state: dict[str, Any], expected_version_token: str, bound_import_digest: str,
    molecule: dict[str, Any], bound_molecule_digest: str,
) -> None:
    if expected_version_token != _version_token(state):
        raise ChemicalPaperError("STALE_CHEMICAL_PAPER_STATE")
    if bound_import_digest != state["current_import_digest"]:
        raise ChemicalPaperError("CHEMICAL_PAPER_IMPORT_STALE")
    if bound_molecule_digest != molecule["molecule_digest"]:
        raise ChemicalPaperError("MOLECULE_BINDING_STALE")


def append_chemical_field_correction(
    project: Path,
    study_id: str,
    molecule_id: str,
    field: str,
    value: str,
    actor: object,
    *,
    reason: str,
    pdf_locator: object,
    expected_version_token: str,
    bound_import_digest: str,
    bound_molecule_digest: str,
    resolution_status: object = None,
    confidence: object = None,
    provenance: object = None,
    gap_reason: object = None,
) -> dict[str, Any]:
    root = _project(project)
    if field not in FIELD_NAMES:
        raise ChemicalPaperError("CHEMICAL_FIELD_INVALID")
    normalized_value = _correction_value(field, value, resolution_status, gap_reason)
    who, why = _actor(actor), _reason(reason)
    try:
        with project_write_lock(root):
            state = load_chemical_paper_state(root, study_id)
            molecule = _molecule(state, molecule_id)
            _require_mutation_binding(state, expected_version_token, bound_import_digest, molecule, bound_molecule_digest)
            current_resolution = _current_resolved_smiles_event(state, molecule)
            if (
                field == "resolved_smiles"
                and current_resolution is not None
                and current_resolution.get("resolution_status") == "CONFIRMED"
            ):
                raise ChemicalPaperError("CONFIRMED_FIELD_IMMUTABLE")
            locator = _correction_locator(
                pdf_locator,
                state["imports"][state["current_import_digest"]]["page_count"],
            )
            resolution_metadata = _resolution_metadata(
                resolution_status, confidence, provenance, who, gap_reason
            )
            prior = _current_value(state, molecule, field)
            event = {
                "molecule_id": molecule["molecule_id"], "field": field, "prior_value": prior, "value": normalized_value,
                "actor": who, "reason": why, "recorded_at": _now(),
                "pdf_locator": locator,
                "bound_import_digest": state["current_import_digest"], "bound_molecule_digest": molecule["molecule_digest"],
                "prior_event_digest": state["field_correction_head_digest"],
                **resolution_metadata,
            }
            event["event_digest"] = canonical_digest(event)
            state["field_corrections"].append(event)
            state["field_correction_head_digest"] = event["event_digest"]
            state["state_digest"] = _canonical_state_digest(state)
            _validate_state(state)
            _atomic_json(_state_path(root, study_id), state)
    except PaperEvidenceStoreError as exc:
        raise ChemicalPaperError(exc.code) from exc
    return {"status": "corrected", "study_id": study_id, "molecule_id": molecule_id, "field": field, "version_token": _version_token(state)}


def correct_chemical_paper_field(
    project: Path,
    *,
    study_id: str,
    molecule_index: int,
    field: str,
    value: str,
    actor: object,
    reason: str,
    pdf_locator: object,
    version_token: str,
    resolution_status: object = None,
    confidence: object = None,
    provenance: object = None,
    gap_reason: object = None,
) -> dict[str, Any]:
    """Apply one safe index-addressed, optimistic-concurrency field correction."""
    state = load_chemical_paper_state(project, study_id)
    molecule = _molecule_by_index(state, molecule_index)
    result = append_chemical_field_correction(
        project,
        study_id,
        molecule["molecule_id"],
        field,
        value,
        actor,
        reason=reason,
        pdf_locator=pdf_locator,
        expected_version_token=version_token,
        bound_import_digest=state["current_import_digest"],
        bound_molecule_digest=molecule["molecule_digest"],
        resolution_status=resolution_status,
        confidence=confidence,
        provenance=provenance,
        gap_reason=gap_reason,
    )
    return {
        "status": result["status"],
        "study_id": study_id,
        "molecule_index": molecule_index,
        "field": field,
        "version_token": result["version_token"],
    }


def _element_counts(value: object) -> dict[str, int]:
    if not isinstance(value, dict) or not value:
        raise ChemicalPaperError("ELEMENT_COUNTS_INVALID")
    result: dict[str, int] = {}
    for key, count in value.items():
        if key not in _ELEMENTS or not isinstance(count, int) or isinstance(count, bool) or count < 1:
            raise ChemicalPaperError("ELEMENT_COUNTS_INVALID")
        result[key] = count
    return dict(sorted(result.items()))


def append_element_review(
    project: Path,
    study_id: str,
    molecule_id: str,
    state_value: str,
    actor: object,
    *,
    reason: str,
    expected_version_token: str,
    bound_import_digest: str,
    bound_molecule_digest: str,
    corrected_counts: dict[str, int] | None = None,
) -> dict[str, Any]:
    root = _project(project)
    if state_value not in ELEMENT_REVIEW_STATES or state_value == "not_reviewed":
        raise ChemicalPaperError("ELEMENT_REVIEW_STATE_INVALID")
    if state_value == "corrected":
        reviewed_counts: dict[str, int] | None = _element_counts(corrected_counts)
    elif corrected_counts is not None:
        raise ChemicalPaperError("ELEMENT_COUNTS_NOT_ALLOWED")
    else:
        reviewed_counts = None
    who, why = _actor(actor), _reason(reason)
    try:
        with project_write_lock(root):
            state = load_chemical_paper_state(root, study_id)
            molecule = _molecule(state, molecule_id)
            _require_mutation_binding(state, expected_version_token, bound_import_digest, molecule, bound_molecule_digest)
            prior_state, prior_counts, _ = _current_element_review(state, molecule)
            current_resolution = _current_resolved_smiles_event(state, molecule)
            event = {
                "molecule_id": molecule["molecule_id"], "prior_state": prior_state, "state": state_value,
                "prior_counts": prior_counts, "reviewed_counts": reviewed_counts,
                "actor": who, "reason": why, "recorded_at": _now(),
                "bound_import_digest": state["current_import_digest"], "bound_molecule_digest": molecule["molecule_digest"],
                "bound_resolution_event_digest": (
                    current_resolution["event_digest"]
                    if current_resolution is not None
                    else None
                ),
                "prior_event_digest": state["element_review_head_digest"],
            }
            event["event_digest"] = canonical_digest(event)
            state["element_reviews"].append(event)
            state["element_review_head_digest"] = event["event_digest"]
            state["state_digest"] = _canonical_state_digest(state)
            _validate_state(state)
            _atomic_json(_state_path(root, study_id), state)
    except PaperEvidenceStoreError as exc:
        raise ChemicalPaperError(exc.code) from exc
    return {"status": state_value, "study_id": study_id, "molecule_id": molecule_id, "version_token": _version_token(state)}


def review_chemical_paper_elements(
    project: Path,
    *,
    study_id: str,
    molecule_index: int,
    review_state: str,
    actor: object,
    reason: str,
    version_token: str,
    corrected_elements: object = None,
) -> dict[str, Any]:
    """Record an optional element review without exposing raw molecule IDs."""
    state = load_chemical_paper_state(project, study_id)
    molecule = _molecule_by_index(state, molecule_index)
    normalized_elements: dict[str, int] | None
    if isinstance(corrected_elements, list):
        normalized_elements = {}
        for row in corrected_elements:
            if not isinstance(row, dict) or set(row) != {"symbol", "count"} or row.get("symbol") in normalized_elements:
                raise ChemicalPaperError("ELEMENT_COUNTS_INVALID")
            normalized_elements[row["symbol"]] = row.get("count")
    elif corrected_elements is None or isinstance(corrected_elements, dict):
        normalized_elements = corrected_elements
    else:
        raise ChemicalPaperError("ELEMENT_COUNTS_INVALID")
    result = append_element_review(
        project,
        study_id,
        molecule["molecule_id"],
        review_state,
        actor,
        reason=reason,
        expected_version_token=version_token,
        bound_import_digest=state["current_import_digest"],
        bound_molecule_digest=molecule["molecule_digest"],
        corrected_counts=normalized_elements,
    )
    return {
        "status": result["status"],
        "study_id": study_id,
        "molecule_index": molecule_index,
        "version_token": result["version_token"],
    }


def _study_summary(
    project: Path,
    project_device: int,
    project_inode: int,
    state: dict[str, Any],
) -> dict[str, Any]:
    unresolved = {field: 0 for field in FIELD_NAMES}
    candidate_difference_count = 0
    invalid_candidate_count = 0
    unreviewed = 0
    molecules: list[dict[str, Any]] = []
    version_token = _version_token(state)
    locator_binding = _source_locator_binding(
        project,
        project_device,
        project_inode,
        state["project_id"],
        state["study_id"],
        state["source_id"],
        state["source_truth_bundle_digest"],
        state["source_pdf_sha256"],
    )
    for molecule_index, molecule in enumerate(state["molecules"]):
        resolution = _resolved_smiles_resolution(state, molecule)
        resolved_smiles = resolution["resolved_smiles"]
        smiles_candidates = _resolved_smiles_details(state, molecule)[1]
        values: dict[str, str | None] = {
            "mol_idt": _current_value(state, molecule, "mol_idt"),
            "resolved_smiles": resolved_smiles,
        }
        for field, current in values.items():
            if current is None:
                unresolved[field] += 1
        candidate_difference_count += int(smiles_candidates["candidate_difference"])
        invalid_candidate_count += sum(
            int(candidate is not None and not _valid_resolved_smiles(candidate))
            for candidate in (smiles_candidates["expanded"], smiles_candidates["unexpanded"])
        )
        review_state, _, _ = _current_element_review(state, molecule)
        if review_state == "not_reviewed":
            unreviewed += 1
        safe_history = []
        for event in [*state["field_corrections"], *state["element_reviews"]]:
            if event["bound_import_digest"] != state["current_import_digest"] or event["molecule_id"] != molecule["molecule_id"]:
                continue
            safe_history.append({
                "kind": "field_correction" if "field" in event else "element_review",
                "field": event.get("field"), "prior_value": event.get("prior_value"), "value": event.get("value"),
                "prior_state": event.get("prior_state"), "state": event.get("state"),
                "actor_type": event["actor"]["actor_type"], "actor_label": event["actor"]["actor_label"],
                "reason": event["reason"], "recorded_at": event["recorded_at"],
                "pdf_locator": event.get("pdf_locator"),
            })
        missing_fields = [field for field in FIELD_NAMES if values[field] is None]
        molecules.append({
            "molecule_index": molecule_index,
            "page": molecule["page_index"] + 1,
            "bbox_normalized": molecule["normalized_bbox"],
            "molblock_available": bool(molecule["element_candidate_counts"]),
            **values,
            "resolved_smiles_status": resolution["resolved_smiles_status"],
            "confidence": resolution["confidence"],
            "provenance": resolution["provenance"],
            "pdf_locator": resolution["pdf_locator"],
            "gap_reason": resolution["gap_reason"],
            "legacy_unclassified": resolution["legacy_unclassified"],
            "smiles_candidates": smiles_candidates,
            "missing_fields": missing_fields,
            "candidate_elements": [
                {"symbol": symbol, "count": count}
                for symbol, count in sorted(molecule["element_candidate_counts"].items())
            ],
            "element_review_state": review_state,
            "pdf_page_url": (
                f"/api/project/{quote(state['project_id'], safe='')}"
                f"/chemical-paper/source/"
                f"{quote(state['source_id'], safe='')}"
                f"/pdf-page?page={molecule['page_index'] + 1}"
                f"&binding={quote(locator_binding, safe='')}"
            ),
            "version_token": version_token,
            "history": safe_history,
        })
    active = state["imports"][state["current_import_digest"]]
    gaps = [f"{count} molecule(s) have unresolved {field}." for field, count in unresolved.items() if count]
    gap_registry = [
        {
            "molecule_index": index,
            "status": "BLOCKED",
            "value": None,
            "gap_reason": row["gap_reason"],
            "pdf_locator": row["pdf_locator"],
        }
        for index, row in enumerate(molecules)
        if row["resolved_smiles_status"] == "BLOCKED"
    ]
    uncertainty_registry = [
        {
            "molecule_index": row["molecule_index"],
            "status": row["resolved_smiles_status"],
            "value": row["resolved_smiles"],
            "confidence": row["confidence"],
            "provenance": row["provenance"],
            "pdf_locator": row["pdf_locator"],
        }
        for row in molecules
        if row["resolved_smiles_status"] in {"CONFIRMED", "AI_PROVISIONAL"}
    ]
    status = "needs_review" if gaps else "ready"
    limitations = [
        "Reaction data was not provided in this export.",
        "No exported image assets were provided; molecule boxes are original-PDF locators only.",
        "Expanded/unexpanded SMILES values are candidate provenance only; the safe projection labels them AI_PROVISIONAL and requires explicit researcher confirmation for CONFIRMED.",
    ]
    if candidate_difference_count:
        limitations.append(
            f"{candidate_difference_count} molecule(s) have an expanded/unexpanded SMILES candidate difference; "
            "resolved_smiles selects the first valid candidate (expanded, then unexpanded) as AI_PROVISIONAL; ambiguous or missing values remain BLOCKED until an explicit resolution is recorded."
        )
    if invalid_candidate_count:
        limitations.append(
            f"{invalid_candidate_count} expanded/unexpanded SMILES value(s) do not pass the resolved SMILES validator and remain provenance candidates, not resolved_smiles."
        )
    return {
        "study_id": state["study_id"], "status": status, "backend": active["backend"],
        "version": active["version"], "pdf_binding_status": "bound",
        "imported_at": active["imported_at"], "page_count": active["page_count"],
        "file_kinds": ["layout", "markdown", "molecule_info"], "molecule_count": len(molecules),
        "reaction_data_status": "unavailable_not_provided", "missing_field_counts": unresolved,
        "resolved_smiles_status_counts": {
            status: sum(row["resolved_smiles_status"] == status for row in molecules)
            for status in ("CONFIRMED", "AI_PROVISIONAL", "BLOCKED")
        },
        "gap_registry": gap_registry,
        "uncertainty_registry": uncertainty_registry,
        "uncertainty_disclosure": (
            "uncertainty_registry discloses classified molecules; "
            "gap_registry contains BLOCKED molecules only."
        ),
        "unreviewed_element_molecule_count": unreviewed, "gaps": gaps,
        "limitations": limitations,
        "version_token": version_token, "molecules": molecules,
    }


def chemical_paper_safe_project_state(project: Path) -> dict[str, Any]:
    root = _project(project)
    project_device, project_inode = _project_instance_identity(root)
    try:
        studies = declared_study_ids(root)
    except SourceTruthError as exc:
        raise ChemicalPaperError(exc.code) from exc
    source_index: ProjectSourceIndex | None = None
    if any(_state_path(root, study_id).exists() for study_id in studies):
        try:
            source_index = build_project_source_index(root)
        except SourceTruthError as exc:
            raise ChemicalPaperError("CHEMICAL_PAPER_SOURCE_TRUTH_STALE") from exc
    summaries: list[dict[str, Any]] = []
    for study_id in studies:
        path = _state_path(root, study_id)
        if path.exists():
            summaries.append(
                _study_summary(
                    root,
                    project_device,
                    project_inode,
                    load_chemical_paper_state(
                        root,
                        study_id,
                        source_index=source_index,
                        declared_studies=studies,
                    )
                )
            )
        else:
            summaries.append({
                "study_id": study_id, "status": "missing", "backend": None,
                "version": None, "pdf_binding_status": "missing", "imported_at": None,
                "page_count": None, "file_kinds": [], "molecule_count": None,
                "reaction_data_status": "unavailable_not_provided",
                "missing_field_counts": None, "unreviewed_element_molecule_count": 0,
                "gaps": ["A valid MinerU Chemical Paper manual export has not been imported."],
                "limitations": ["Reaction data was not provided in this export."], "version_token": None, "molecules": [],
            })
    imported = [row for row in summaries if row["status"] != "missing"]
    project_status = (
        "missing" if summaries and not imported
        else "ready" if len(imported) == len(summaries) and all(row["status"] == "ready" for row in imported)
        else "needs_review"
    )
    molecule_count = sum(int(row["molecule_count"] or 0) for row in imported)
    unresolved_count = sum(
        sum((row["missing_field_counts"] or {}).values()) for row in imported
    )
    return {
        "schema_version": "chemical-paper-projection.v2",
        "route": "chemical-paper-zip-only",
        "project_status": project_status,
        "summary": {
            "studies": len(summaries),
            "imported": len(imported),
            "molecules": molecule_count,
            "unresolved_fields": unresolved_count,
            "reaction_data_status": "unavailable_not_provided",
        },
        "studies": summaries,
    }


def chemical_paper_projection(project: Path) -> dict[str, Any]:
    return chemical_paper_safe_project_state(project)


def chemical_paper_manuscript_bindings(project: Path) -> dict[str, Any]:
    """Return the exact frozen v2 manuscript provenance fields."""
    root = _project(project)
    try:
        study_ids = declared_study_ids(root)
    except SourceTruthError as exc:
        raise ChemicalPaperError(exc.code) from exc
    import_rows: list[dict[str, str]] = []
    molecule_count = 0
    missing_name_count = 0
    missing_resolved_smiles_count = 0
    review_counts = {state: 0 for state in ("not_reviewed", "confirmed", "corrected", "not_applicable")}
    for study_id in study_ids:
        if not _state_path(root, study_id).is_file():
            continue
        state = load_chemical_paper_state(root, study_id)
        import_rows.append(
            {
                "study_id": study_id,
                "import_digest": state["current_import_digest"],
                "state_digest": state["state_digest"],
            }
        )
        molecule_count += len(state["molecules"])
        for molecule in state["molecules"]:
            missing_name_count += _current_value(state, molecule, "mol_idt") is None
            missing_resolved_smiles_count += (
                _current_value(state, molecule, "resolved_smiles") is None
            )
            review_state, _, _ = _current_element_review(state, molecule)
            review_counts[review_state] += 1
    import_rows.sort(key=lambda row: row["study_id"])
    return {
        "chemical_paper_import_digests": import_rows,
        "chemical_paper_safe_summary": {
            "schema_version": "chemical-paper-safe-summary.v2",
            "route": "chemical-paper-zip-only",
            "study_count": len(import_rows),
            "molecule_count": molecule_count,
            "missing_name_count": missing_name_count,
            "missing_resolved_smiles_count": missing_resolved_smiles_count,
            "ai_authored_smiles_count": 0,
            "element_review_counts": review_counts,
            "reaction_data_status": "unavailable_not_provided",
        },
    }


def _validated_import_digest_rows(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ChemicalPaperError("CHEMICAL_PAPER_LINEAGE_INVALID")
    rows: list[dict[str, str]] = []
    for row in value:
        if not isinstance(row, dict) or set(row) != {"study_id", "import_digest", "state_digest"}:
            raise ChemicalPaperError("CHEMICAL_PAPER_LINEAGE_INVALID")
        rows.append(
            {
                "study_id": _identifier(row.get("study_id"), "CHEMICAL_PAPER_LINEAGE_INVALID"),
                "import_digest": _digest(row.get("import_digest"), "CHEMICAL_PAPER_LINEAGE_INVALID"),
                "state_digest": _digest(row.get("state_digest"), "CHEMICAL_PAPER_LINEAGE_INVALID"),
            }
        )
    if rows != sorted(rows, key=lambda row: row["study_id"]) or len({row["study_id"] for row in rows}) != len(rows):
        raise ChemicalPaperError("CHEMICAL_PAPER_LINEAGE_INVALID")
    return rows


def _validated_claim_dependency_rows(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ChemicalPaperError("CHEMICAL_PAPER_CLAIM_DEPENDENCY_INVALID")
    rows: list[dict[str, Any]] = []
    required_keys = {
        "claim_id", "study_id", "molecule_index", "required_fields",
        "requires_element_review", "requires_reaction_data",
    }
    for row in value:
        if not isinstance(row, dict) or set(row) != required_keys:
            raise ChemicalPaperError("CHEMICAL_PAPER_CLAIM_DEPENDENCY_INVALID")
        fields = row.get("required_fields")
        if (
            not isinstance(fields, list)
            or fields != sorted(fields)
            or len(fields) != len(set(fields))
            or not set(fields) <= set(FIELD_NAMES)
        ):
            raise ChemicalPaperError("CHEMICAL_PAPER_CLAIM_DEPENDENCY_INVALID")
        molecule_index = row.get("molecule_index")
        if not isinstance(molecule_index, int) or isinstance(molecule_index, bool) or molecule_index < 0:
            raise ChemicalPaperError("CHEMICAL_PAPER_CLAIM_DEPENDENCY_INVALID")
        if not isinstance(row.get("requires_element_review"), bool) or not isinstance(row.get("requires_reaction_data"), bool):
            raise ChemicalPaperError("CHEMICAL_PAPER_CLAIM_DEPENDENCY_INVALID")
        rows.append(
            {
                "claim_id": _identifier(row.get("claim_id"), "CHEMICAL_PAPER_CLAIM_DEPENDENCY_INVALID"),
                "study_id": _identifier(row.get("study_id"), "CHEMICAL_PAPER_CLAIM_DEPENDENCY_INVALID"),
                "molecule_index": molecule_index,
                "required_fields": list(fields),
                "requires_element_review": row["requires_element_review"],
                "requires_reaction_data": row["requires_reaction_data"],
            }
        )
    return rows


def chemical_paper_dependency_currentness(
    project: Path,
    *,
    import_digests: object,
    claim_dependencies: object,
) -> dict[str, Any]:
    """Resolve release currentness without making Release parse private state."""
    root = _project(project)
    import_rows = _validated_import_digest_rows(import_digests)
    dependency_rows = _validated_claim_dependency_rows(claim_dependencies)
    binding_by_study = {row["study_id"]: row for row in import_rows}
    states: dict[str, dict[str, Any]] = {}
    study_status: dict[str, str] = {}
    for study_id, binding in binding_by_study.items():
        try:
            state = load_chemical_paper_state(root, study_id)
        except ChemicalPaperError as exc:
            study_status[study_id] = "missing" if exc.code == "CHEMICAL_PAPER_NOT_IMPORTED" else "stale"
            continue
        states[study_id] = state
        study_status[study_id] = (
            "current"
            if binding["import_digest"] == state["current_import_digest"]
            and binding["state_digest"] == state["state_digest"]
            else "stale"
        )
    if not import_rows and dependency_rows:
        lineage_status = "missing"
    elif any(value == "stale" for value in study_status.values()):
        lineage_status = "stale"
    elif any(value == "missing" for value in study_status.values()):
        lineage_status = "missing"
    else:
        lineage_status = "current"

    by_claim: dict[str, list[dict[str, Any]]] = {}
    for dependency in dependency_rows:
        claim_id = dependency["claim_id"]
        study_id = dependency["study_id"]
        blocking: list[str] = []
        required_statuses: dict[str, str] = {}
        state = states.get(study_id)
        status = study_status.get(study_id, "missing")
        element_state = "not_reviewed"
        reaction_status = "unavailable_not_provided"
        if status == "current" and state is not None:
            try:
                molecule = _molecule_by_index(state, dependency["molecule_index"])
            except ChemicalPaperError:
                status = "missing"
            else:
                resolved_smiles_confirmed = False
                for field in dependency["required_fields"]:
                    value = _current_value(state, molecule, field)
                    corrected = any(
                        event["bound_import_digest"] == state["current_import_digest"]
                        and event["molecule_id"] == molecule["molecule_id"]
                        and event["field"] == field
                        for event in state["field_corrections"]
                    )
                    if field == "resolved_smiles":
                        resolution_status = _resolved_smiles_resolution(
                            state, molecule
                        )["resolved_smiles_status"]
                        if resolution_status == "AI_PROVISIONAL":
                            field_status = "unresolved"
                            blocking.append(
                                f"{claim_id}:resolved_smiles:provisional_not_confirmed"
                            )
                        elif resolution_status != "CONFIRMED":
                            field_status = "unresolved"
                        else:
                            resolved_smiles_confirmed = True
                            field_status = "corrected" if corrected else "resolved"
                    else:
                        field_status = "corrected" if corrected else ("resolved" if value is not None else "unresolved")
                    required_statuses[field] = field_status
                    if field_status == "unresolved" and not (
                        field == "resolved_smiles"
                        and resolution_status == "AI_PROVISIONAL"
                    ):
                        unresolved_reason = f"{claim_id}:{field}:unresolved"
                        if unresolved_reason not in blocking:
                            blocking.append(unresolved_reason)
                element_state, _, _ = _current_element_review(state, molecule)
                material_dependency = (
                    "resolved_smiles" in dependency["required_fields"]
                    and resolved_smiles_confirmed
                )
                if material_dependency:
                    if element_state == "not_reviewed":
                        blocking.append(f"{claim_id}:elements:not_reviewed")
                    elif element_state not in {"confirmed", "corrected"}:
                        blocking.append(f"{claim_id}:elements:not_confirmed")
                    else:
                        resolution_event = _current_resolved_smiles_event(state, molecule)
                        review_event = _current_element_review_event(state, molecule)
                        if (
                            resolution_event is None
                            or review_event is None
                            or review_event.get("bound_import_digest")
                            != state["current_import_digest"]
                            or review_event.get("molecule_id")
                            != molecule["molecule_id"]
                            or review_event.get("bound_molecule_digest")
                            != molecule["molecule_digest"]
                            or resolution_event.get("bound_import_digest")
                            != state["current_import_digest"]
                            or resolution_event.get("molecule_id")
                            != molecule["molecule_id"]
                            or resolution_event.get("bound_molecule_digest")
                            != molecule["molecule_digest"]
                            or review_event.get("bound_resolution_event_digest")
                            != resolution_event.get("event_digest")
                        ):
                            blocking.append(
                                f"{claim_id}:elements:resolution_review_stale"
                            )
                        elif _actor_identity(resolution_event.get("actor")) == _actor_identity(
                            review_event.get("actor")
                        ):
                            blocking.append(f"{claim_id}:elements:not_independent")
                elif dependency["requires_element_review"] and element_state == "not_reviewed":
                    blocking.append(f"{claim_id}:elements:not_reviewed")
                if dependency["requires_reaction_data"]:
                    blocking.append(f"{claim_id}:reaction_data:unavailable_not_provided")
                status = "unavailable" if dependency["requires_reaction_data"] else ("needs_review" if blocking else "current")
        if status in {"stale", "missing"}:
            blocking.append(f"{claim_id}:chemical_paper:{status}")
        row = {
            "study_id": study_id,
            "molecule_index": dependency["molecule_index"],
            "status": status,
            "required_field_statuses": required_statuses,
            "element_review_state": element_state,
            "reaction_data_status": reaction_status,
            "blocking_reasons": sorted(set(blocking)),
        }
        by_claim.setdefault(claim_id, []).append(row)

    claims: list[dict[str, Any]] = []
    top_blocking: list[str] = []
    priority = {"stale": 4, "missing": 3, "unavailable": 2, "needs_review": 1, "current": 0}
    for claim_id in sorted(by_claim):
        dependencies = sorted(by_claim[claim_id], key=lambda row: (row["study_id"], row["molecule_index"]))
        status = max((row["status"] for row in dependencies), key=lambda value: priority[value])
        blocking = sorted({reason for row in dependencies for reason in row["blocking_reasons"]})
        claims.append({"claim_id": claim_id, "status": status, "dependencies": dependencies, "blocking_reasons": blocking})
        top_blocking.extend(blocking)
    if lineage_status != "current":
        top_blocking.append(f"chemical_paper_import_digests:{lineage_status}")
    top_blocking = sorted(set(top_blocking))
    return {
        "schema_version": "chemical-paper-dependency-currentness.v2",
        "lineage_binding_status": lineage_status,
        "claims": claims,
        "can_release": lineage_status == "current" and all(row["status"] == "current" for row in claims),
        "blocking_reasons": top_blocking,
    }


def _resolution_digest(state: dict[str, Any]) -> str:
    rows = []
    for molecule in state["molecules"]:
        row = {
            "molecule_id": molecule["molecule_id"],
            **{field: _current_value(state, molecule, field) for field in FIELD_NAMES},
        }
        row["resolved_smiles_status"] = _resolved_smiles_resolution(
            state, molecule
        )["resolved_smiles_status"]
        rows.append(row)
    return canonical_digest(rows)


def _element_review_digest(state: dict[str, Any]) -> str:
    rows = []
    for molecule in state["molecules"]:
        review_state, counts, event_digest = _current_element_review(state, molecule)
        rows.append({"molecule_id": molecule["molecule_id"], "state": review_state, "counts": counts, "event_digest": event_digest})
    return canonical_digest(rows)


def chemical_dependency_state(project: Path, evidence_id: str, dependencies: object) -> dict[str, Any]:
    evidence_id = _identifier(evidence_id, "EVIDENCE_ID_INVALID")
    if not isinstance(dependencies, list):
        raise ChemicalPaperError("CHEMICAL_DEPENDENCY_INVALID")
    rows: list[dict[str, Any]] = []
    gaps: list[str] = []
    statuses: list[str] = []
    for dependency in dependencies:
        if not isinstance(dependency, dict) or set(dependency) != {"study_id", "molecule_id", "molecule_digest", "chemical_paper_import_digest", "required_fields"}:
            raise ChemicalPaperError("CHEMICAL_DEPENDENCY_INVALID")
        state = load_chemical_paper_state(project, dependency["study_id"])
        molecule = _molecule(state, dependency["molecule_id"])
        if dependency["chemical_paper_import_digest"] != state["current_import_digest"]:
            raise ChemicalPaperError("CHEMICAL_PAPER_IMPORT_STALE")
        if dependency["molecule_digest"] != molecule["molecule_digest"]:
            raise ChemicalPaperError("MOLECULE_BINDING_STALE")
        required = dependency["required_fields"]
        if not isinstance(required, list) or len(required) != len(set(required)) or not set(required) <= REQUIRED_FIELD_NAMES:
            raise ChemicalPaperError("CHEMICAL_DEPENDENCY_INVALID")
        status = "ready"
        for field in required:
            if field in FIELD_NAMES:
                if field == "resolved_smiles":
                    resolution_status = _resolved_smiles_resolution(
                        state, molecule
                    )["resolved_smiles_status"]
                    if resolution_status == "AI_PROVISIONAL":
                        gaps.append(
                            f"{state['study_id']}/{molecule['molecule_id']}:resolved_smiles:provisional_not_confirmed"
                        )
                        status = "blocked_unresolved"
                    elif resolution_status != "CONFIRMED":
                        gaps.append(
                            f"{state['study_id']}/{molecule['molecule_id']}:{field}:unresolved"
                        )
                        status = "blocked_unresolved"
                elif _current_value(state, molecule, field) is None:
                    gaps.append(f"{state['study_id']}/{molecule['molecule_id']}:{field}:unresolved")
                    status = "blocked_unresolved"
        review_state, _, review_digest = _current_element_review(state, molecule)
        if "elements" in required and review_state == "not_reviewed":
            gaps.append(f"{state['study_id']}/{molecule['molecule_id']}:elements:not_reviewed")
            if status == "ready":
                status = "blocked_unreviewed"
        row = {
            "evidence_id": evidence_id, "study_id": state["study_id"], "molecule_id": molecule["molecule_id"],
            "molecule_digest": molecule["molecule_digest"], "chemical_paper_import_digest": state["current_import_digest"],
            "required_fields": sorted(required), "field_resolution_digest": _resolution_digest(state),
            "element_review_digest": review_digest, "dependency_status": status,
        }
        row["dependency_digest"] = canonical_digest({key: value for key, value in row.items() if key != "dependency_status"})
        rows.append(row)
        statuses.append(status)
    overall = "blocked_unresolved" if "blocked_unresolved" in statuses else ("blocked_unreviewed" if "blocked_unreviewed" in statuses else "ready")
    return {"evidence_id": evidence_id, "dependency_status": overall, "dependencies": rows, "gaps": sorted(gaps)}
