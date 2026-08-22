#!/usr/bin/env python3
"""Build a deterministic QoderWork CN Expert Kit ZIP from this package."""

from __future__ import annotations

import argparse
import json
import re
import stat
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "qoderwork/plugins/review-writer-cn"
STAMP = (1980, 1, 1, 0, 0, 0)
FORBIDDEN_PARTS = {".git", ".env", "__pycache__"}
SECRET_RE = re.compile(r"(?i)(sk-[A-Za-z0-9_-]{12,}|AKIA[0-9A-Z]{12,})")


def validate_plugin(plugin: Path = PLUGIN) -> list[Path]:
    manifest_path = plugin / ".qoder-plugin/plugin.json"
    if not manifest_path.is_file() or not (plugin / "skills").is_dir():
        raise ValueError("plugin must contain .qoder-plugin/plugin.json and skills/")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("name") != plugin.name or not isinstance(manifest.get("skills"), list):
        raise ValueError("plugin manifest name/skills are invalid")
    files: list[Path] = []
    for path in sorted(plugin.rglob("*")):
        relative = path.relative_to(plugin)
        if path.is_symlink() or any(part in FORBIDDEN_PARTS for part in relative.parts):
            raise ValueError(f"forbidden plugin path: {relative.as_posix()}")
        if path.is_file():
            if path.suffix.lower() not in {".json", ".md"}:
                raise ValueError(f"unsupported plugin file: {relative.as_posix()}")
            text = path.read_text(encoding="utf-8")
            if SECRET_RE.search(text):
                raise ValueError(f"secret-like value in: {relative.as_posix()}")
            files.append(path)
    expected = {f"./skills/{path.parent.name}" for path in (plugin / "skills").glob("*/SKILL.md")}
    if set(manifest["skills"]) != expected:
        raise ValueError("manifest skills do not match packaged skills")
    return files


def build(output: Path, plugin: Path = PLUGIN) -> Path:
    files = validate_plugin(plugin)
    destination = output if output.is_absolute() else ROOT / output
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for source in files:
            entry = zipfile.ZipInfo(source.relative_to(plugin).as_posix(), STAMP)
            entry.create_system = 3
            entry.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(entry, source.read_bytes(), compress_type=zipfile.ZIP_DEFLATED)
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the Review Writer CN Expert Kit")
    parser.add_argument("--output", type=Path, default=Path("build/review-writer-cn.qoder-plugin.zip"))
    args = parser.parse_args()
    print(build(args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
