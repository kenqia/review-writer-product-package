#!/usr/bin/env python3
"""Build deterministic reading-order and visual-locator text layers with pdftotext."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import tempfile
from pathlib import Path


SOURCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def decode_pdftotext_utf8(raw: bytes) -> str:
    """Decode UTF-8, losslessly normalizing paired CESU-8 surrogates when present."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        surrogate_text = raw.decode("utf-8", errors="surrogatepass")
        return surrogate_text.encode("utf-16-le", errors="surrogatepass").decode("utf-16-le")


def page_count(path: Path) -> int:
    pages = path.read_text(encoding="utf-8").split("\f")
    if pages and not pages[-1].strip():
        pages.pop()
    return len(pages)


def parse_source(value: str) -> tuple[str, Path]:
    source_id, separator, raw_path = value.partition("=")
    if not separator or not SOURCE_ID_RE.fullmatch(source_id) or not raw_path:
        raise argparse.ArgumentTypeError("--source must be SOURCE_ID=/path/to/source.pdf")
    path = Path(raw_path).resolve()
    if not path.is_file() or path.suffix.casefold() != ".pdf":
        raise argparse.ArgumentTypeError(f"PDF source does not exist: {path}")
    return source_id, path


def run_pdftotext(executable: Path, source: Path, destination: Path, *, layout: bool) -> None:
    command = [str(executable)]
    if layout:
        command.append("-layout")
    command.extend(["-enc", "UTF-8", str(source), str(destination)])
    subprocess.run(command, check=True, capture_output=True, text=True)
    raw = destination.read_bytes()
    normalized = decode_pdftotext_utf8(raw).encode("utf-8")
    if normalized != raw:
        destination.write_bytes(normalized)


def build_layers(
    sources: list[tuple[str, Path]],
    output_root: Path,
    pdftotext: Path,
    *,
    force: bool,
) -> dict:
    output_root.mkdir(parents=True, exist_ok=True)
    manifest_path = output_root / "text_layers.manifest.json"
    if force and manifest_path.exists():
        try:
            existing_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            existing_rows = existing_manifest["sources"]
            existing_ids = {
                row["source_id"]
                for row in existing_rows
                if isinstance(row, dict) and isinstance(row.get("source_id"), str)
            }
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise RuntimeError("existing text-layer manifest is invalid") from exc
        requested_ids = {source_id for source_id, _ in sources}
        if existing_ids - requested_ids:
            raise RuntimeError(
                "refusing to drop existing source layers; pass every existing and new --source together"
            )
    planned = [
        output_root / name
        for source_id, _ in sources
        for name in (f"{source_id}.reading.txt", f"{source_id}.layout.txt")
    ]
    planned.append(manifest_path)
    existing = [path for path in planned if path.exists()]
    if existing and not force:
        raise FileExistsError(f"refusing to overwrite existing output: {existing[0]}")

    manifest_sources = []
    # Stage beside the destination so the final atomic replace never crosses
    # filesystems when TMPDIR points at Windows and output_root is in WSL (or
    # vice versa).
    with tempfile.TemporaryDirectory(prefix=".pdf-layers-", dir=output_root) as temp_dir:
        temp_root = Path(temp_dir)
        staged: list[tuple[Path, Path]] = []
        for source_id, pdf_path in sources:
            reading_name = f"{source_id}.reading.txt"
            layout_name = f"{source_id}.layout.txt"
            reading_temp = temp_root / reading_name
            layout_temp = temp_root / layout_name
            run_pdftotext(pdftotext, pdf_path, reading_temp, layout=False)
            run_pdftotext(pdftotext, pdf_path, layout_temp, layout=True)
            reading_pages = page_count(reading_temp)
            layout_pages = page_count(layout_temp)
            if reading_pages < 1 or reading_pages != layout_pages:
                raise RuntimeError(
                    f"text-layer page count mismatch for {source_id}: "
                    f"reading={reading_pages}, layout={layout_pages}"
                )
            manifest_sources.append(
                {
                    "source_id": source_id,
                    "pdf_name": pdf_path.name,
                    "pdf_sha256": sha256_file(pdf_path),
                    "page_count": reading_pages,
                    "reading_order_path": reading_name,
                    "reading_order_sha256": sha256_file(reading_temp),
                    "reading_order_method": "pdftotext-default-reading-order",
                    "layout_path": layout_name,
                    "layout_sha256": sha256_file(layout_temp),
                    "layout_method": "pdftotext-layout-visual-locator-only",
                }
            )
            staged.extend(
                [
                    (reading_temp, output_root / reading_name),
                    (layout_temp, output_root / layout_name),
                ]
            )

        manifest = {
            "schema_version": "pdf-text-layers.v1",
            "sources": manifest_sources,
        }
        manifest_temp = temp_root / "text_layers.manifest.json"
        manifest_temp.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        staged.append((manifest_temp, output_root / manifest_temp.name))
        for source, destination in staged:
            source.replace(destination)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Create a default pdftotext reading-order layer for exact quotes and a "
            "-layout layer only for figure/table/visual location."
        )
    )
    parser.add_argument("--source", action="append", required=True, type=parse_source)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--pdftotext", default=Path("pdftotext"), type=Path)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    try:
        build_layers(
            args.source,
            args.output_root.resolve(),
            args.pdftotext,
            force=args.force,
        )
    except (FileExistsError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        parser.exit(2, f"error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
