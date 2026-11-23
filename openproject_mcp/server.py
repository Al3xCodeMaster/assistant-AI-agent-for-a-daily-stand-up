"""OpenProject MCP server (remote, Streamable HTTP, bearer token).

Anthropic's MCP connector calls it from the cloud, so it must sit behind public HTTPS (reverse proxy
or tunnel), same as gcal_mcp. Tools:
  list_blocked_work_packages(project_id), get_work_package(work_package_id),
  list_project_users(project_id), update_work_package(work_package_id, status_id, assignee_id),
  add_comment(work_package_id, text), create_work_package(project_id, subject, description, assignee_id)

Run:  python -m openproject_mcp.server      # http://0.0.0.0:9200/mcp
"""
import hmac
import os

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

from openproject_mcp.openproject import OpenProject


class BearerAuth:
    """Pure ASGI wrapper: every HTTP request needs `Authorization: Bearer <token>`."""

    def __init__(self, app, token: str):
        if not token:
            raise ValueError("OPENPROJECT_MCP_TOKEN must be set")
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            header = dict(scope["headers"]).get(b"authorization", b"").decode()
            if not hmac.compare_digest(header, f"Bearer {self.token}"):
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def build_app(op: OpenProject, token: str):
    # DNS-rebinding protection only allows localhost, which would reject the public host.
    # The bearer token is the protection here.
    mcp = FastMCP(
        "openproject",
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @mcp.tool()
    async def list_blocked_work_packages(project_id: str) -> dict:
        """List open work packages of a project that are blocked by an unfinished work package (id, subject, status, assignee, blocked_by hrefs)."""
        return {"blocked": await op.list_blocked_work_packages(project_id)}

    @mcp.tool()
    async def get_work_package(work_package_id: str) -> dict:
        """Return id, subject, status, assignee of one work package."""
        return await op.get_work_package(work_package_id)

    @mcp.tool()
    async def list_statuses() -> dict:
        """List the work package statuses (id, name, closed). Use the id as status_id in update_work_package."""
        return {"statuses": await op.list_statuses()}

    @mcp.tool()
    async def list_project_users(project_id: str) -> dict:
        """List users who can be assigned work in the project (id, name, login)."""
        return {"users": await op.list_project_users(project_id)}

    @mcp.tool()
    async def update_work_package(work_package_id: str, status_id: str | None = None,
                                  assignee_id: str | None = None) -> dict:
        """Change the status id and/or the assignee user id of a work package."""
        return await op.update_work_package(work_package_id, status_id, assignee_id)

    @mcp.tool()
    async def add_comment(work_package_id: str, text: str) -> dict:
        """Post a comment on a work package (use for meeting notes, metrics and unblock plans)."""
        return await op.add_comment(work_package_id, text)

    @mcp.tool()
    async def create_work_package(project_id: str, subject: str, description: str = "",
                                  assignee_id: str | None = None) -> dict:
        """Create a follow-up work package in a project."""
        return await op.create_work_package(project_id, subject, description, assignee_id)

    return BearerAuth(mcp.streamable_http_app(), token)


def main() -> None:
    import uvicorn

    op = OpenProject(
        base_url=os.environ.get("OPENPROJECT_URL", ""),
        api_key=os.environ.get("OPENPROJECT_API_KEY", ""),
    )
    app = build_app(op, os.environ.get("OPENPROJECT_MCP_TOKEN", ""))
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("OPENPROJECT_MCP_PORT", "9200")))


if __name__ == "__main__":
    main()
