---
name: review-orchestrator
description: Create or resume one local source-bound chemistry review when the user supplies a topic, an explicit project root, and an authorized local PDF folder.
---

# Review Orchestrator

Use this skill for one review project only. The project directory is the sole
durable authority for sources, Evidence, manuscript, figures, evaluations,
versions, and exports. Do not use the superseded global
`review-writing-orchestrator` skill or create a parallel store.

## Required Input

Obtain a topic, an explicit project root, and an authorized local PDF folder
from a visible Codex conversation. The user can request a fresh project or say
“从该 root 继续” for a resume. Do not discover, download, or add papers. Do not ask the user to run generator-start,
generator-continue, terminal commands, cURL, pytest, or helper scripts: the native Agent invokes repository tools
itself and reports the resulting project state and clickable Dashboard URL.

## Native Agent Flow

1. Resolve the explicit project root. For a fresh project, create only the
   project-scoped authority from the supplied PDFs. For a resume, read its
   current VersionContext and source/Evidence bindings before writing.
2. Use the existing source, Evidence, draft, figure, quality, release, and
   resume producers. After parse-quality approval, the Agent calls
   `parse_project_sources` and then routes PDF-only, non-chemical observations
   through `register_pdf_only_evidence`. That tool records an explicit
   Chemical `GAP`/unsupported limitation with `field_dependencies=[]`; it
   never manufactures a Chemical Paper archive. Claims that depend on
   molecule/SMILES/molblock still use the strict Chemical import gate.
   Preserve source hashes, locators, stale checks, user ownership markers,
   and pointer-last VersionContext semantics. Do not invent scientific
   evidence, bypass a human decision, or create a Provider, RAG, schema,
   store, or workflow engine.
3. Record the native Agent session/run and tool outcomes in the existing
   project-owned seams. Generate the English v1 only from current,
   source-bound Evidence. `GeneratorSession` is the current owner of the
   v1 -> human decision -> marker-preserving v2 loop.
4. When a current producer returns `HUMAN_ACTION_REQUIRED`, start or reuse the
   existing local Dashboard for the project's parent review root. Return its
   real `http://127.0.0.1:<port>/review?project_id=<project ID>` URL and pause.
   The Dashboard is for
   human reading, editing, verification, decisions, and downloads; it never
   wakes the Agent or becomes a second authority.
5. When the user says to continue from the same root, read the same project
   directory and its recorded decision. Generate v2 without overwriting `USER_EDITED` or
   `RESEARCHER_AUTHORED` text, then use existing figure, quality, same-version
   Markdown/DOCX, stale/regenerate, History branch/undo, and cold-resume
   public seams where their preconditions are met.

## Fail Closed

Reject missing, duplicate, wrong-role, corrupt, stale, or unbound inputs
before a write. Do not move Stable, PROMOTE, B2, or any protected project.
If a required public seam is absent, report that first blocker and preserve
the project unchanged. Never claim PUBLIC_E2E, HUMAN_ACCEPTANCE, or scientific
validity from bounded synthetic or engineering checks.
