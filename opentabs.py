"""
Minimal client for the OpenTabs MCP server.

OpenTabs runs as a local HTTP server (default 127.0.0.1:9515) that drives the
user's own Chrome, so anything it opens carries their existing logins. That is
the only reason this tool can read Stellic at all: the audit sits behind
Microsoft SSO and there is no unauthenticated path to it.

This speaks just enough of MCP's streamable-HTTP transport to call tools -
initialize, notifications/initialized, tools/call - with the standard library
and nothing else. Responses arrive either as plain JSON or as Server-Sent
Events, so both are handled.

The bearer token lives in the user's ~/.claude.json. It is read at runtime and
never copied into this repo.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:9515/mcp"
PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "advisingbot", "version": "2.0"}


class OpenTabsError(RuntimeError):
    """Raised when OpenTabs is unreachable or a tool call fails."""


def _find_token() -> str | None:
    """Pull the OpenTabs bearer token out of the user's Claude config.

    The token appears under any project's mcpServers entry; they are all the
    same server, so the first one that looks right wins.
    """
    override = os.environ.get("OPENTABS_TOKEN")
    if override:
        return override if override.startswith("Bearer ") else f"Bearer {override}"

    config = Path.home() / ".claude.json"
    if not config.exists():
        return None
    try:
        data = json.loads(config.read_text())
    except (json.JSONDecodeError, OSError):
        return None

    found: list[str] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if (
                    key == "opentabs"
                    and isinstance(value, dict)
                    and isinstance(value.get("headers"), dict)
                ):
                    auth = value["headers"].get("Authorization")
                    if auth:
                        found.append(auth)
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return found[0] if found else None


def _parse_sse(body: str) -> dict:
    """Pull the JSON-RPC payload out of an SSE response."""
    for line in body.splitlines():
        if line.startswith("data:"):
            chunk = line[5:].strip()
            if chunk:
                return json.loads(chunk)
    raise OpenTabsError(f"No data frame in SSE response: {body[:200]!r}")


class OpenTabs:
    """A session against the OpenTabs MCP server."""

    def __init__(
        self,
        url: str = DEFAULT_URL,
        *,
        token: str | None = None,
        timeout: int = 60,
        max_retries: int = 5,
    ) -> None:
        self.url = os.environ.get("OPENTABS_URL", url)
        self.timeout = timeout
        self.max_retries = max_retries
        self.token = token or _find_token()
        if not self.token:
            raise OpenTabsError(
                "No OpenTabs token found. Expected an `opentabs` entry with an "
                "Authorization header in ~/.claude.json, or set OPENTABS_TOKEN."
            )
        self._session_id: str | None = None
        self._next_id = 0
        self._ready = False
        self._last_call = 0.0
        self.min_interval = 0.15

    # -- transport --------------------------------------------------------

    def _send(self, method: str, params: dict | None = None, *, notify=False):
        """Send one JSON-RPC message, backing off if OpenTabs rate-limits us."""
        for attempt in range(self.max_retries):
            try:
                return self._send_once(method, params, notify=notify)
            except OpenTabsError as exc:
                retry_after = getattr(exc, "retry_after", None)
                if retry_after is None or attempt + 1 >= self.max_retries:
                    raise
                time.sleep(retry_after)
        raise OpenTabsError(f"{method}: gave up after {self.max_retries} tries")

    def _send_once(self, method: str, params: dict | None = None, *, notify=False):
        self._next_id += 1
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        if not notify:
            message["id"] = self._next_id

        headers = {
            "Authorization": self.token,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

        request = urllib.request.Request(
            self.url,
            data=json.dumps(message).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        gap = time.monotonic() - self._last_call
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)
        self._last_call = time.monotonic()

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                session = response.headers.get("Mcp-Session-Id")
                if session:
                    self._session_id = session
                raw = response.read().decode("utf-8", "replace")
                content_type = response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:300].decode("utf-8", "replace")
            error = OpenTabsError(f"{exc.code} from OpenTabs: {detail}")
            if exc.code == 429:
                # OpenTabs throttles bursts; wait it out rather than failing.
                header = exc.headers.get("Retry-After")
                try:
                    error.retry_after = float(header) if header else 2.0
                except ValueError:
                    error.retry_after = 2.0
            raise error from exc
        except urllib.error.URLError as exc:
            raise OpenTabsError(
                f"Could not reach OpenTabs at {self.url} ({exc.reason}). "
                "Is the OpenTabs app running?"
            ) from exc

        if notify or not raw.strip():
            return None

        payload = (
            _parse_sse(raw) if "text/event-stream" in content_type
            else json.loads(raw)
        )
        if "error" in payload:
            error = payload["error"]
            raise OpenTabsError(
                f"{method} failed: {error.get('message')} "
                f"({error.get('code')})"
            )
        return payload.get("result")

    def connect(self) -> dict:
        """Handshake. Safe to call more than once."""
        if self._ready:
            return {}
        result = self._send(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": CLIENT_INFO,
            },
        )
        self._send("notifications/initialized", {}, notify=True)
        self._ready = True
        return result or {}

    # -- tools ------------------------------------------------------------

    def call(self, tool: str, arguments: dict | None = None):
        """Call a tool and return its decoded result.

        MCP wraps tool output in a content list; the useful part is almost
        always a single JSON text block, so unwrap it when we can.
        """
        self.connect()
        result = self._send(
            "tools/call", {"name": tool, "arguments": arguments or {}}
        )
        if not isinstance(result, dict):
            return result

        if result.get("isError"):
            raise OpenTabsError(
                f"{tool}: {_text_of(result) or 'tool reported an error'}"
            )
        if "structuredContent" in result:
            return result["structuredContent"]

        text = _text_of(result)
        if text is None:
            return result
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text


def _text_of(result: dict) -> str | None:
    parts = [
        block.get("text", "")
        for block in result.get("content") or []
        if block.get("type") == "text"
    ]
    return "\n".join(parts) if parts else None


# -- browser conveniences -------------------------------------------------


# Chrome refuses tab edits while the user is mid-drag, and a tab can vanish
# between calls. Both clear on their own within a moment.
TRANSIENT = ("cannot be edited", "No tab with id", "Target closed")


def is_transient(error: Exception) -> bool:
    return any(marker in str(error) for marker in TRANSIENT)


def open_tab(session: OpenTabs, url: str, *, attempts: int = 4) -> int:
    for attempt in range(attempts):
        try:
            tab = session.call("browser_open_tab", {"url": url})
        except OpenTabsError as exc:
            if is_transient(exc) and attempt + 1 < attempts:
                time.sleep(0.8 * (attempt + 1))
                continue
            raise
        if not isinstance(tab, dict) or "id" not in tab:
            raise OpenTabsError(f"Unexpected browser_open_tab result: {tab!r}")
        return int(tab["id"])
    raise OpenTabsError(f"Could not open a tab for {url}")


def close_tab(session: OpenTabs, tab_id: int) -> None:
    try:
        session.call("browser_close_tab", {"tabId": tab_id})
    except OpenTabsError:
        # Never let cleanup failure mask a real error from the caller.
        pass


def wait_for_load(
    session: OpenTabs, tab_id: int, *, timeout: float = 45.0
) -> dict:
    """Block until the tab reports a complete load."""
    deadline = time.monotonic() + timeout
    info: dict = {}
    while time.monotonic() < deadline:
        info = session.call("browser_get_tab_info", {"tabId": tab_id}) or {}
        if info.get("status") == "complete":
            return info
        time.sleep(0.4)
    raise OpenTabsError(
        f"Tab did not finish loading within {timeout:.0f}s "
        f"(last status: {info.get('status')!r})"
    )


def run_script(session: OpenTabs, tab_id: int, script: str):
    """Evaluate JavaScript in the page and return its value.

    Used to talk to a site's own API from inside the authenticated page,
    which is far steadier than scraping rendered HTML.
    """
    result = session.call(
        "browser_execute_script", {"tabId": tab_id, "code": script}
    )
    # OpenTabs nests the evaluated value two levels deep.
    while isinstance(result, dict) and set(result) == {"value"}:
        result = result["value"]
    return result


def ping(url: str = DEFAULT_URL) -> str:
    """One-line health check, for the CLI and for setup docs."""
    info = OpenTabs(url).connect()
    server = (info or {}).get("serverInfo") or {}
    return (
        f"connected to {server.get('name', 'opentabs')} "
        f"v{server.get('version', '?')}"
    )


if __name__ == "__main__":
    print(ping())
