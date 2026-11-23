"""OpenProject API v3 calls for the OpenProject MCP server.

Plain httpx, no MCP imports, so it can be tested without the MCP SDK. Auth is HTTP basic with
user `apikey` and the user's API token as password (OpenProject's documented scheme).
Blocked work packages are the open ones that have a "blocked" relation pointing at an unfinished
blocker (see `blockers_of`).
"""
import json

import httpx

PAGE_SIZE = 100
DEFAULT_TYPE_ID = "1"  # "Task" in a default OpenProject install; override per project if needed


def blockers_of(work_package_href: str, relations: list[dict]) -> list[str]:
    """Hrefs of work packages that block the given one, from its relations.

    OpenProject stores relations directed: a "blocks" relation from A to B means A blocks B, which
    is shown as "blocked" from B's side. We accept both spellings so either side of the link works.
    """
    blockers = []
    for rel in relations:
        kind = rel.get("type")
        frm = rel.get("_links", {}).get("from", {}).get("href")
        to = rel.get("_links", {}).get("to", {}).get("href")
        if kind == "blocked" and frm == work_package_href and to:
            blockers.append(to)
        elif kind == "blocks" and to == work_package_href and frm:
            blockers.append(frm)
    return blockers


def _title(wp: dict, link: str) -> str:
    """Name of a linked resource. Collections only carry it in _links.<link>.title, single items embed it."""
    embedded = (wp.get("_embedded", {}).get(link) or {}).get("name")
    return embedded or wp.get("_links", {}).get(link, {}).get("title", "") or ""


class OpenProject:
    def __init__(self, base_url: str, api_key: str, http: httpx.AsyncClient | None = None):
        if not (base_url and api_key):
            raise ValueError("OPENPROJECT_URL and OPENPROJECT_API_KEY must be set")
        self.api = base_url.rstrip("/") + "/api/v3"
        self.auth = ("apikey", api_key)
        self.http = http or httpx.AsyncClient(timeout=20)

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        r = await self.http.request(method, f"{self.api}{path}", auth=self.auth, **kwargs)
        r.raise_for_status()
        return r.json() if r.content else {}

    async def _paged_elements(self, path: str, filters: list[dict] | None = None) -> list[dict]:
        """All `_embedded.elements` of a collection, following offset pagination."""
        out: list[dict] = []
        offset = 1
        while True:
            params = {"pageSize": str(PAGE_SIZE), "offset": str(offset)}
            if filters:
                params["filters"] = json.dumps(filters)
            data = await self._request("GET", path, params=params)
            elements = data.get("_embedded", {}).get("elements", [])
            out.extend(elements)
            total = data.get("total", len(out))
            if not elements or len(out) >= total:
                return out
            offset += 1

    async def list_statuses(self) -> list[dict]:
        """Work package statuses of the instance: id (use as status_id), name, closed flag."""
        statuses = await self._paged_elements("/statuses")
        return [{"id": s["id"], "name": s.get("name", ""), "closed": bool(s.get("isClosed"))} for s in statuses]

    async def get_work_package(self, wp_id: str) -> dict:
        wp = await self._request("GET", f"/work_packages/{wp_id}")
        return {
            "id": wp["id"],
            "subject": wp.get("subject", ""),
            "status": _title(wp, "status"),
            "assignee": _title(wp, "assignee") or None,
            "lockVersion": wp.get("lockVersion"),
            "href": wp["_links"]["self"]["href"],
        }

    async def list_blocked_work_packages(self, project_id: str) -> list[dict]:
        """Open work packages in the project that have at least one unfinished blocker."""
        open_filter = [{"status": {"operator": "o", "values": []}}]
        open_wps = await self._paged_elements(f"/projects/{project_id}/work_packages", open_filter)
        blocked = []
        for wp in open_wps:
            href = wp["_links"]["self"]["href"]
            relations = await self._paged_elements(f"/work_packages/{wp['id']}/relations")
            blockers = blockers_of(href, relations)
            if blockers:
                blocked.append({
                    "id": wp["id"],
                    "subject": wp.get("subject", ""),
                    "status": _title(wp, "status"),
                    "assignee": _title(wp, "assignee") or None,
                    "blocked_by": blockers,
                })
        return blocked

    async def list_project_users(self, project_id: str) -> list[dict]:
        users = await self._paged_elements(f"/projects/{project_id}/available_assignees")
        return [{"id": u["id"], "name": u.get("name", ""), "login": u.get("login", "")} for u in users]

    async def add_comment(self, wp_id: str, text: str) -> dict:
        data = await self._request("POST", f"/work_packages/{wp_id}/activities",
                                   json={"comment": {"raw": text}})
        return {"id": data.get("id"), "work_package_id": wp_id}

    async def update_work_package(self, wp_id: str, status_id: str | None = None,
                                  assignee_id: str | None = None) -> dict:
        if status_id is None and assignee_id is None:
            raise ValueError("update_work_package needs status_id or assignee_id")
        current = await self.get_work_package(wp_id)  # OpenProject requires the current lockVersion
        links: dict = {}
        if status_id is not None:
            links["status"] = {"href": f"/api/v3/statuses/{status_id}"}
        if assignee_id is not None:
            links["assignee"] = {"href": f"/api/v3/users/{assignee_id}"}
        data = await self._request("PATCH", f"/work_packages/{wp_id}",
                                   json={"lockVersion": current["lockVersion"], "_links": links})
        return {"id": data.get("id", wp_id), "subject": data.get("subject", "")}

    async def create_work_package(self, project_id: str, subject: str, description: str = "",
                                  assignee_id: str | None = None, type_id: str = DEFAULT_TYPE_ID) -> dict:
        links: dict = {"type": {"href": f"/api/v3/types/{type_id}"}}
        if assignee_id is not None:
            links["assignee"] = {"href": f"/api/v3/users/{assignee_id}"}
        data = await self._request("POST", f"/projects/{project_id}/work_packages", json={
            "subject": subject,
            "description": {"format": "markdown", "raw": description},
            "_links": links,
        })
        return {"id": data.get("id"), "subject": data.get("subject", subject)}
