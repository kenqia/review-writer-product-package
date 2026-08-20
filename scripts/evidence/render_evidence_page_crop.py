#!/usr/bin/env python3
"""Render one deterministic full-page evidence crop and bind its provenance."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from evidence_atom_core import (
    EvidenceAtomCoreError,
    RENDERER_CONTRACT,
    packet_path,
    render_pdf_page,
    sha256_file,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Render one hash-bound full PDF page.")
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--source-pdf", required=True, type=Path)
    parser.add_argument("--page", required=True, type=int)
    parser.add_argument("--renderer", required=True, type=Path)
    parser.add_argument("--packet-root", required=True, type=Path)
    parser.add_argument("--asset-path", required=True)
    parser.add_argument("--manifest-output", required=True, type=Path)
    args = parser.parse_args()
    try:
        packet_root = args.packet_root.resolve()
        asset = packet_path(packet_root, args.asset_path)
        manifest_output = args.manifest_output.resolve()
        manifest_output.relative_to(packet_root)
        render_pdf_page(args.source_pdf, args.page, args.renderer, asset)
        manifest = {
            "schema_version": "evidence-page-crop-manifest.v1",
            "source_id": args.source_id,
            "source_binary_sha256": sha256_file(args.source_pdf),
            "page": args.page,
            "renderer_contract": RENDERER_CONTRACT,
            "renderer_sha256": sha256_file(args.renderer),
            "asset_path": args.asset_path,
            "asset_sha256": sha256_file(asset),
        }
    except (EvidenceAtomCoreError, OSError, ValueError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1
    manifest_output.parent.mkdir(parents=True, exist_ok=True)
    manifest_output.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
