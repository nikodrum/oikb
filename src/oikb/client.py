"""HTTP client wrapping the Open WebUI Knowledge Base sync API."""

from __future__ import annotations

import json
from typing import Any

import httpx


class OikbClient:
    """Stateless HTTP client for the Open WebUI KB API.

    All methods are synchronous — httpx handles connection pooling internally.
    """

    def __init__(self, base_url: str, token: str, timeout: float = 120.0):
        self._base_url = base_url.rstrip("/")
        self._http = httpx.Client(
            base_url=f"{self._base_url}/api/v1",
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )

    def __enter__(self) -> OikbClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    # ── Sync API ────────────────────────────────────────────────

    def sync_diff(
        self,
        kb_id: str,
        manifest: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """POST /knowledge/{id}/sync/diff — compute diff from manifest."""
        resp = self._http.post(
            f"/knowledge/{kb_id}/sync/diff",
            json={"manifest": manifest},
        )
        resp.raise_for_status()
        return resp.json()

    def sync_cleanup(
        self,
        kb_id: str,
        file_ids: list[str],
        dir_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """POST /knowledge/{id}/sync/cleanup — remove stale files and dirs."""
        payload: dict[str, Any] = {"file_ids": file_ids}
        if dir_ids:
            payload["dir_ids"] = dir_ids
        resp = self._http.post(
            f"/knowledge/{kb_id}/sync/cleanup",
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()

    # ── File upload ─────────────────────────────────────────────

    def upload_file(
        self,
        file_content: bytes,
        filename: str,
        kb_id: str,
        file_hash: str,
        directory_id: str | None = None,
    ) -> dict[str, Any]:
        """POST /files/ — upload a single file to the KB."""

        metadata: dict[str, Any] = {
            "knowledge_id": kb_id,
            "file_hash": file_hash,
        }
        if directory_id:
            metadata["directory_id"] = directory_id

        resp = self._http.post(
            "/files/",
            files={"file": (filename, file_content)},
            data={"metadata": json.dumps(metadata)},
        )
        resp.raise_for_status()
        return resp.json()

    # ── Directory management ────────────────────────────────────

    def create_directory(
        self,
        kb_id: str,
        name: str,
        parent_id: str | None = None,
    ) -> dict[str, Any]:
        """POST /knowledge/{id}/dirs/create — create a directory."""
        payload: dict[str, Any] = {"name": name}
        if parent_id:
            payload["parent_id"] = parent_id
        resp = self._http.post(
            f"/knowledge/{kb_id}/dirs/create",
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()

    # ── KB management ───────────────────────────────────────────

    def reset_kb(
        self,
        kb_id: str,
        include_directories: bool = True,
    ) -> dict[str, Any]:
        """POST /knowledge/{id}/reset — reset the KB."""
        resp = self._http.post(
            f"/knowledge/{kb_id}/reset",
            params={"include_directories": include_directories},
        )
        resp.raise_for_status()
        return resp.json()

    def list_knowledge_bases(self) -> list[dict[str, Any]]:
        """GET /knowledge/ — list all Knowledge Bases the user can access.

        The Open WebUI endpoint paginates (``{"items": [...], "total": N}``,
        ~30 per page via ``?page=N``); some builds return a bare list instead.
        This walks every page and returns the combined list. Each item
        includes at least ``id`` and ``name``.
        """
        all_items: list[dict[str, Any]] = []
        page = 1
        while page <= 1000:  # hard cap against a misbehaving server
            resp = self._http.get("/knowledge/", params={"page": page})
            resp.raise_for_status()
            data = resp.json()

            if isinstance(data, list):
                return data  # non-paginated server — the whole list at once

            items = data.get("items") or data.get("knowledge") or data.get("data") or []
            if not items:
                break
            all_items.extend(items)

            total = data.get("total")
            if total is not None and len(all_items) >= total:
                break
            page += 1

        return all_items

    def get_kb(self, kb_id: str) -> dict[str, Any]:
        """GET /knowledge/{id} — get KB info."""
        resp = self._http.get(f"/knowledge/{kb_id}")
        resp.raise_for_status()
        return resp.json()

    def get_kb_file_count(self, kb_id: str) -> int:
        """Number of files currently in a KB.

        Uses GET /knowledge/{id}/files, whose ``total`` field is the true
        count. (GET /knowledge/{id} returns ``files: null`` on current Open
        WebUI builds, so it cannot be used for this.)
        """
        resp = self._http.get(f"/knowledge/{kb_id}/files", params={"page": 1})
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            if data.get("total") is not None:
                return int(data["total"])
            items = data.get("items") or data.get("files") or []
            return len(items)
        return len(data) if isinstance(data, list) else 0

    def list_kb_files(self, kb_id: str) -> list[dict[str, Any]]:
        """GET /knowledge/{id}/files — list files in a KB."""
        resp = self._http.get(f"/knowledge/{kb_id}")
        resp.raise_for_status()
        data = resp.json()
        return data.get("files", [])
