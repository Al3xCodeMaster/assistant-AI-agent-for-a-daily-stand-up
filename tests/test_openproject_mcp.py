"""Tests for the OpenProject MCP server (openproject_mcp/).

OpenProject is faked with httpx.MockTransport: no network, no credentials.
The client tests need only httpx; the server auth test needs the MCP SDK and is skipped without it.
Run:  python3 -m unittest tests.test_openproject_mcp
"""
import asyncio
import json
import os
import sys
import unittest

import httpx

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

from openproject_mcp.openproject import OpenProject, blockers_of  # noqa: E402

BASE = "https://op.example.com"
API = f"{BASE}/api/v3"


def wp(id_, subject, status="Open", assignee=None):
    embedded = {"status": {"name": status}}
    if assignee:
        embedded["assignee"] = {"name": assignee}
    return {"id": id_, "subject": subject, "lockVersion": 3,
            "_links": {"self": {"href": f"/api/v3/work_packages/{id_}"}}, "_embedded": embedded}


def rel(kind, frm, to):
    return {"type": kind, "_links": {"from": {"href": f"/api/v3/work_packages/{frm}"},
                                      "to": {"href": f"/api/v3/work_packages/{to}"}}}


def fake_openproject(routes: dict, calls: list | None = None):
    """routes: {(METHOD, path): json_body or callable(request)->body}. Records every request."""
    log = calls if calls is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        log.append(request)
        key = (request.method, request.url.path.removeprefix("/api/v3"))
        if key not in routes:
            return httpx.Response(404, json={"message": "not found"})
        body = routes[key]
        return httpx.Response(200, json=body(request) if callable(body) else body)

    return handler, log


def make_client(handler):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenProject(BASE, "secret-key", http=http)


class BlockersTests(unittest.TestCase):
    def test_blocked_relation_points_to_blocker(self):
        me = "/api/v3/work_packages/10"
        rels = [rel("blocked", 10, 7)]
        self.assertEqual(blockers_of(me, rels), ["/api/v3/work_packages/7"])

    def test_blocks_relation_from_other_side(self):
        me = "/api/v3/work_packages/10"
        rels = [rel("blocks", 7, 10)]
        self.assertEqual(blockers_of(me, rels), ["/api/v3/work_packages/7"])

    def test_unrelated_relations_ignored(self):
        me = "/api/v3/work_packages/10"
        rels = [rel("relates", 10, 7), rel("blocked", 8, 10)]  # 10 blocks 8, not blocked by it
        self.assertEqual(blockers_of(me, rels), [])


class ClientTests(unittest.TestCase):
    def test_lists_only_open_work_packages_that_are_blocked(self):
        open_list = {"total": 2, "_embedded": {"elements": [wp(10, "Login HU", assignee="Ana"), wp(11, "Reportes HU")]}}
        routes = {
            ("GET", "/projects/5/work_packages"): open_list,
            ("GET", "/work_packages/10/relations"): {"total": 1, "_embedded": {"elements": [rel("blocked", 10, 7)]}},
            ("GET", "/work_packages/11/relations"): {"total": 0, "_embedded": {"elements": []}},
        }
        handler, log = fake_openproject(routes)
        result = asyncio.run(make_client(handler).list_blocked_work_packages("5"))
        self.assertEqual(result, [{
            "id": 10, "subject": "Login HU", "status": "Open", "assignee": "Ana",
            "blocked_by": ["/api/v3/work_packages/7"],
        }])
        # the open filter is sent and auth is basic apikey:<key>
        self.assertIn("filters", log[0].url.params)
        self.assertTrue(log[0].headers["authorization"].startswith("Basic "))

    def test_pagination_follows_offset(self):
        page1 = {"total": 2, "_embedded": {"elements": [wp(1, "A")]}}
        page2 = {"total": 2, "_embedded": {"elements": [wp(2, "B")]}}

        def users(request):
            return page2 if request.url.params.get("offset") == "2" else page1

        routes = {
            ("GET", "/projects/5/available_assignees"): users,
        }
        handler, _ = fake_openproject(routes)
        result = asyncio.run(make_client(handler).list_project_users("5"))
        self.assertEqual([u["id"] for u in result], [1, 2])

    def test_update_reads_lock_version_then_patches(self):
        routes = {
            ("GET", "/work_packages/10"): wp(10, "Login HU"),
            ("PATCH", "/work_packages/10"): lambda r: {"id": 10, "subject": "Login HU"},
        }
        handler, log = fake_openproject(routes)
        asyncio.run(make_client(handler).update_work_package("10", status_id="7", assignee_id="42"))
        patch = [r for r in log if r.method == "PATCH"][0]
        body = json.loads(patch.content)
        self.assertEqual(body["lockVersion"], 3)
        self.assertEqual(body["_links"]["status"]["href"], "/api/v3/statuses/7")
        self.assertEqual(body["_links"]["assignee"]["href"], "/api/v3/users/42")

    def test_update_requires_a_change(self):
        handler, _ = fake_openproject({})
        with self.assertRaises(ValueError):
            asyncio.run(make_client(handler).update_work_package("10"))

    def test_add_comment_posts_raw_markdown(self):
        routes = {("POST", "/work_packages/10/activities"): {"id": 99}}
        handler, log = fake_openproject(routes)
        result = asyncio.run(make_client(handler).add_comment("10", "Plan: paso 1"))
        self.assertEqual(json.loads(log[0].content), {"comment": {"raw": "Plan: paso 1"}})
        self.assertEqual(result, {"id": 99, "work_package_id": "10"})

    def test_create_work_package_sends_type_and_assignee(self):
        routes = {("POST", "/projects/5/work_packages"): {"id": 12, "subject": "Seguimiento"}}
        handler, log = fake_openproject(routes)
        result = asyncio.run(make_client(handler).create_work_package("5", "Seguimiento", "Detalle", "42"))
        body = json.loads(log[0].content)
        self.assertEqual(body["subject"], "Seguimiento")
        self.assertEqual(body["_links"]["type"]["href"], "/api/v3/types/1")
        self.assertEqual(body["_links"]["assignee"]["href"], "/api/v3/users/42")
        self.assertEqual(result, {"id": 12, "subject": "Seguimiento"})

    def test_lists_statuses_with_ids(self):
        routes = {("GET", "/statuses"): {"total": 2, "_embedded": {"elements": [
            {"id": 1, "name": "New", "isClosed": False},
            {"id": 5, "name": "Closed", "isClosed": True},
        ]}}}
        handler, _ = fake_openproject(routes)
        result = asyncio.run(make_client(handler).list_statuses())
        self.assertEqual(result, [{"id": 1, "name": "New", "closed": False},
                                  {"id": 5, "name": "Closed", "closed": True}])

    def test_missing_credentials_refused(self):
        with self.assertRaises(ValueError):
            OpenProject("", "key")


class ServerAuthTests(unittest.TestCase):
    def test_requests_without_bearer_token_rejected(self):
        try:
            from starlette.testclient import TestClient

            from openproject_mcp.server import build_app
        except ImportError as exc:  # pragma: no cover - MCP SDK not installed
            self.skipTest(f"MCP SDK missing: {exc}")
        handler, _ = fake_openproject({})
        app = build_app(make_client(handler), "op-test-token")
        with TestClient(app) as client:
            self.assertEqual(client.post("/mcp", headers={"authorization": "Bearer wrong"}).status_code, 401)
            self.assertEqual(client.post("/mcp").status_code, 401)

    def test_server_refuses_empty_token(self):
        try:
            from openproject_mcp.server import BearerAuth
        except ImportError as exc:  # pragma: no cover
            self.skipTest(f"MCP SDK missing: {exc}")
        with self.assertRaises(ValueError):
            BearerAuth(None, "")


if __name__ == "__main__":
    unittest.main()
