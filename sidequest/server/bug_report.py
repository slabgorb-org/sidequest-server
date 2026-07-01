"""POST /api/bug-report — the in-app bug reporter's server endpoint.

Flow: validate → upload attachments to R2 (hard dep) → scrub + gather log/OTEL
(best-effort, absence recorded) → compose Markdown → create GitHub issue (hard
dep) → emit ``bug_report.created`` → return the issue URL.
"""
from __future__ import annotations

import json
import logging
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from starlette.concurrency import run_in_threadpool

from sidequest.server.bug_report_enrich import compose_body, otel_summary, scrub, tail_server_log
from sidequest.server.github_issue import DEFAULT_LABELS, GitHubIssueError, create_issue
from sidequest.server.r2_upload import R2UploadError, object_key, upload_bytes
from sidequest.telemetry.watcher_hub import publish_event

logger = logging.getLogger(__name__)

MAX_FILES = 6
MAX_FILE_MB = 10
MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024
_ALLOWED_EXT = (".log", ".txt", ".json")


def _is_image(f: UploadFile) -> bool:
    return (f.content_type or "").startswith("image/")


def _allowed(f: UploadFile) -> bool:
    if _is_image(f):
        return True
    return (f.filename or "").lower().endswith(_ALLOWED_EXT)


def register_bug_report_routes(router: APIRouter) -> None:
    @router.post("/api/bug-report", status_code=201)
    async def create_bug_report(
        title: str = Form(...),
        description: str = Form(...),
        session_slug: str = Form(""),
        context_json: str = Form("{}"),
        files: list[UploadFile] = File(default=[]),  # noqa: B008 — fastapi DI pattern; File() isn't in ruff's bugbear allowlist
    ) -> dict[str, Any]:
        if not title.strip() or not description.strip():
            raise HTTPException(status_code=400, detail="title and description are required")
        if len(files) > MAX_FILES:
            raise HTTPException(status_code=400, detail=f"at most {MAX_FILES} files")

        # Validate every file BEFORE uploading any, so a later bad file never
        # leaves an orphaned R2 object from an earlier successful upload.
        for f in files:
            if f.size is not None and f.size > MAX_FILE_BYTES:
                raise HTTPException(status_code=400, detail=f"{f.filename or 'file'} exceeds {MAX_FILE_MB} MB")
            if not _allowed(f):
                raise HTTPException(status_code=400, detail=f"{f.filename or 'file'}: unsupported type")

        report_id = uuid4().hex
        attachments: list[tuple[str, str, bool]] = []
        for i, f in enumerate(files):
            data = await f.read()
            if len(data) > MAX_FILE_BYTES:  # backstop when f.size was unavailable
                raise HTTPException(status_code=400, detail=f"{f.filename or 'file'} exceeds {MAX_FILE_MB} MB")
            key = object_key(report_id, i, f.filename or "file")
            try:
                url = await run_in_threadpool(
                    upload_bytes, key, data, f.content_type or "application/octet-stream"
                )
            except R2UploadError as exc:
                raise HTTPException(status_code=502, detail=f"attachment upload failed: {exc}") from exc
            attachments.append((f.filename or key, url, _is_image(f)))

        try:
            context = json.loads(context_json) if context_json else {}
            if not isinstance(context, dict):
                context = {}
        except json.JSONDecodeError:
            context = {}

        raw_log = await run_in_threadpool(tail_server_log)
        log_text = scrub(raw_log) if raw_log is not None else None
        raw_otel = await otel_summary(session_slug)
        otel_text = scrub(raw_otel) if raw_otel is not None else None

        body = compose_body(
            description=description,
            context=context,
            attachments=attachments,
            log_text=log_text,
            otel_text=otel_text,
            report_id=report_id,
            session_slug=session_slug,
        )

        try:
            issue = await create_issue(title.strip(), body, list(DEFAULT_LABELS))
        except GitHubIssueError as exc:
            raise HTTPException(status_code=502, detail=f"GitHub issue creation failed: {exc}") from exc

        publish_event(
            "bug_report.created",
            {
                "report_id": report_id,
                "issue_number": issue["number"],
                "file_count": len(attachments),
                "session_slug": session_slug,
            },
            component="bug_report",
        )
        logger.info(
            "bug_report.created report_id=%s issue=%s files=%d",
            report_id,
            issue["number"],
            len(attachments),
        )
        return {"issue_url": issue["url"], "issue_number": issue["number"], "report_id": report_id}
