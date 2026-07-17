"""Minimal MCP server exposing Microsoft Graph (SharePoint + OneDrive) to Claude Code.

Setup:
    1. Register an app in Microsoft Entra ID (Azure portal > App registrations > New).
       - Supported account types: "Accounts in this organizational directory only" (single tenant)
         or "Any work or school account" (multitenant).
       - Under Authentication > Advanced settings, enable "Allow public client flows" = Yes.
       - Under API permissions, add delegated Microsoft Graph: User.Read, Files.Read.All,
         Sites.Read.All. (Narrower scopes like Files.Read work without admin consent if
         broader ones are denied.)
    2. Copy the Application (client) ID and Directory (tenant) ID into a .env file
       (see .env.example) or export them as environment variables.
    3. python -m venv venv && venv\\Scripts\\activate (Windows) or source venv/bin/activate
    4. pip install -r requirements.txt
    5. python server.py --login    # one-time browser sign-in, caches token
    6. Register with Claude Code:
         claude mcp add sharepoint -- python C:\\path\\to\\server.py
"""
import json
import os
import sys
from pathlib import Path

import httpx
import msal
from mcp.server.fastmcp import FastMCP

CLIENT_ID = os.environ.get("GRAPH_CLIENT_ID")
TENANT_ID = os.environ.get("GRAPH_TENANT_ID", "organizations")
SCOPES = os.environ.get(
    "GRAPH_SCOPES", "User.Read Files.Read.All Sites.Read.All"
).split()
CACHE_PATH = Path(
    os.environ.get(
        "GRAPH_TOKEN_CACHE", str(Path.home() / ".mcp_sharepoint_token_cache.bin")
    )
)

AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"

INTERACTIVE = False


def _load_cache() -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    if CACHE_PATH.exists():
        cache.deserialize(CACHE_PATH.read_text())
    return cache


def _save_cache(cache: msal.SerializableTokenCache) -> None:
    if cache.has_state_changed:
        CACHE_PATH.write_text(cache.serialize())


def _get_token() -> str:
    if not CLIENT_ID:
        raise RuntimeError("GRAPH_CLIENT_ID environment variable is required.")
    cache = _load_cache()
    app = msal.PublicClientApplication(
        CLIENT_ID, authority=AUTHORITY, token_cache=cache
    )
    accounts = app.get_accounts()
    result = None
    if accounts:
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
    if not result:
        if not INTERACTIVE:
            raise RuntimeError(
                "Not signed in. Run `python server.py --login` first to authenticate."
            )
        flow = app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            raise RuntimeError(f"Failed to initiate device flow: {flow}")
        print(flow["message"], flush=True)
        result = app.acquire_token_by_device_flow(flow)
    _save_cache(cache)
    if "access_token" not in result:
        raise RuntimeError(
            f"Auth failed: {result.get('error_description', result)}"
        )
    return result["access_token"]


def _graph_get(path: str, params: dict | None = None) -> dict:
    token = _get_token()
    r = httpx.get(
        f"{GRAPH_BASE}{path}",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def _graph_post(path: str, body: dict) -> dict:
    token = _get_token()
    r = httpx.post(
        f"{GRAPH_BASE}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def _graph_get_bytes(path: str) -> bytes:
    token = _get_token()
    r = httpx.get(
        f"{GRAPH_BASE}{path}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
        follow_redirects=True,
    )
    r.raise_for_status()
    return r.content


mcp = FastMCP("sharepoint")


@mcp.tool()
def search(query: str, limit: int = 10) -> str:
    """Search SharePoint and OneDrive for files matching the query.

    Returns JSON list of {name, webUrl, driveId, itemId, lastModified, summary}.
    Use driveId + itemId with read_file() to fetch contents.
    """
    body = {
        "requests": [
            {
                "entityTypes": ["driveItem"],
                "query": {"queryString": query},
                "from": 0,
                "size": min(max(limit, 1), 25),
            }
        ]
    }
    data = _graph_post("/search/query", body)
    hits = []
    for resp in data.get("value", []):
        for container in resp.get("hitsContainers", []):
            for hit in container.get("hits", []):
                res = hit.get("resource", {})
                ref = res.get("parentReference") or {}
                hits.append(
                    {
                        "name": res.get("name"),
                        "webUrl": res.get("webUrl"),
                        "driveId": ref.get("driveId"),
                        "itemId": res.get("id"),
                        "lastModified": res.get("lastModifiedDateTime"),
                        "summary": hit.get("summary"),
                    }
                )
    return json.dumps(hits, indent=2)


@mcp.tool()
def recent_files(limit: int = 10) -> str:
    """List the signed-in user's recently accessed files."""
    data = _graph_get("/me/drive/recent", params={"$top": min(max(limit, 1), 50)})
    items = []
    for i in data.get("value", []):
        remote = i.get("remoteItem") or {}
        ref = remote.get("parentReference") or i.get("parentReference") or {}
        items.append(
            {
                "name": i.get("name"),
                "webUrl": i.get("webUrl"),
                "driveId": ref.get("driveId"),
                "itemId": remote.get("id") or i.get("id"),
                "lastModified": i.get("lastModifiedDateTime"),
            }
        )
    return json.dumps(items, indent=2)


@mcp.tool()
def list_sites(query: str = "") -> str:
    """List SharePoint sites the user can access. Optional query filters by name."""
    data = _graph_get("/sites", params={"search": query or "*"})
    sites = [
        {
            "id": s.get("id"),
            "name": s.get("displayName") or s.get("name"),
            "webUrl": s.get("webUrl"),
        }
        for s in data.get("value", [])
    ]
    return json.dumps(sites, indent=2)


@mcp.tool()
def read_file(drive_id: str, item_id: str, max_chars: int = 50000) -> str:
    """Download a file's content as text. Use driveId/itemId from search() or recent_files()."""
    content = _graph_get_bytes(f"/drives/{drive_id}/items/{item_id}/content")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return f"[binary file, {len(content)} bytes — not UTF-8 decodable]"
    if len(text) > max_chars:
        return text[:max_chars] + f"\n\n[truncated — {len(text) - max_chars} more chars]"
    return text


if __name__ == "__main__":
    if "--login" in sys.argv:
        INTERACTIVE = True
        _get_token()
        print("Signed in. Token cached at", CACHE_PATH)
    else:
        mcp.run()
