"""Minimal MCP client (Model Context Protocol) - standard library only.

Speaks JSON-RPC 2.0 over two transports:
  stdio - newline-delimited JSON to a child process (npx / uvx / python ...)
  http  - Streamable HTTP: POST JSON-RPC, answer as JSON or an SSE stream

Only what the assistant needs: initialize, tools/list, tools/call.
Synchronous and thread-safe; failures raise McpError, and McpHub converts
them into board-tool-style {"ok": False, "error": ...} dicts.

SECURITY MODEL. An MCP server is an ordinary program running with your user
rights - only add servers you trust. Every MCP tool call asks the user for
confirmation (Yes/No, default No) unless that server's config sets
"confirm": false. The agent therefore cannot touch files, network or anything
else behind MCP without an explicit per-call approval by default.

MCP tools do not touch the QGraphicsScene, so they record no board history.

Self-test (spawns a tiny demo stdio server, no network, no packages):
    python -m refsynapse.ai.mcp_client
Run the demo server itself (for wiring tests from the settings dialog):
    python -m refsynapse.ai.mcp_client --serve
"""

import json
import os
import shlex
import subprocess
import sys
import threading
import urllib.error
import urllib.request

PROTOCOL_VERSION = "2024-11-05"
CLIENT_INFO = {"name": "RefSynapse", "version": "0.4"}
CONNECT_TIMEOUT = 15.0
CALL_TIMEOUT = 90.0
NAME_PREFIX = "mcp__"


class McpError(Exception):
    """Server refused, timed out or answered with something unusable."""


def sanitize(text):
    cleaned = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in str(text))
    return cleaned[:24] or "server"


def split_args(text):
    """Command-line arguments from one line; keeps Windows backslashes,
    strips surrounding quotes so paths with spaces survive."""
    try:
        tokens = shlex.split(text or "", posix=False)
    except ValueError:
        tokens = (text or "").split()
    out = []
    for token in tokens:
        if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
            token = token[1:-1]
        out.append(token)
    return out


# ------------------------------------------------------------------ stdio
class StdioClient:
    """Newline-delimited JSON-RPC over a child process's stdin/stdout."""

    def __init__(self, command, args=(), env=None, cwd=None):
        self.command = [command] + list(args or [])
        # MCP stdio is UTF-8 by spec; Python children on Windows would
        # otherwise decode our UTF-8 as the locale codepage (CP1251...).
        self.env = dict(os.environ, PYTHONIOENCODING="utf-8", **(env or {}))
        self.cwd = cwd or None
        self._proc = None
        self._write_lock = threading.Lock()
        self._lock = threading.Lock()
        self._next_id = 0
        self._pending = {}

    def start(self):
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            self._proc = subprocess.Popen(
                self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, env=self.env, cwd=self.cwd,
                creationflags=flags)
        except OSError as ex:
            raise McpError("Cannot start '%s': %s" % (self.command[0], ex)) from None
        threading.Thread(target=self._read_loop, daemon=True).start()

    def _read_loop(self):
        for raw in self._proc.stdout:
            try:
                msg = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            if "id" in msg and ("result" in msg or "error" in msg):
                with self._lock:
                    slot = self._pending.pop(msg["id"], None)
                if slot is not None:
                    slot["msg"] = msg
                    slot["event"].set()
            elif "method" in msg and "id" in msg:
                # server -> client request: answer ping, refuse the rest
                if msg["method"] == "ping":
                    self._send({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
                else:
                    self._send({"jsonrpc": "2.0", "id": msg["id"],
                                "error": {"code": -32601, "message": "Method not supported"}})
        with self._lock:                      # process died: wake every waiter
            slots = list(self._pending.values())
            self._pending.clear()
        for slot in slots:
            slot["msg"] = {"error": {"message": "MCP server process exited"}}
            slot["event"].set()

    def _send(self, obj):
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        with self._write_lock:
            if self._proc is None or self._proc.stdin is None:
                raise McpError("MCP server is not running")
            try:
                self._proc.stdin.write(data)
                self._proc.stdin.flush()
            except OSError as ex:
                raise McpError("MCP server pipe broken: %s" % ex) from None

    def request(self, method, params=None, timeout=CALL_TIMEOUT):
        with self._lock:
            self._next_id += 1
            rid = self._next_id
            slot = {"event": threading.Event()}
            self._pending[rid] = slot
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        if not slot["event"].wait(timeout):
            with self._lock:
                self._pending.pop(rid, None)
            raise McpError("MCP '%s' timed out after %ss" % (method, timeout))
        msg = slot["msg"]
        if "error" in msg:
            raise McpError(str(msg["error"].get("message", msg["error"])))
        return msg.get("result") or {}

    def notify(self, method, params=None):
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def close(self):
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


# ------------------------------------------------------------------- http
class HttpClient:
    """Streamable HTTP transport: one POST per message, JSON or SSE answer."""

    def __init__(self, url, headers=None):
        self.url = url
        self.headers = dict(headers or {})
        self.session = None
        self._next_id = 0
        self._lock = threading.Lock()

    def start(self):
        pass                                   # stateless until initialize

    def _post(self, obj, timeout):
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        headers.update(self.headers)
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        request = urllib.request.Request(self.url, data=json.dumps(obj).encode("utf-8"),
                                         headers=headers, method="POST")
        try:
            resp = urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as ex:
            body = ex.read().decode("utf-8", "replace")[:300]
            raise McpError("MCP HTTP %d: %s" % (ex.code, body)) from None
        except (urllib.error.URLError, OSError, TimeoutError) as ex:
            raise McpError("MCP HTTP connection failed: %s" % ex) from None
        with resp:
            sid = resp.headers.get("Mcp-Session-Id")
            if sid:
                self.session = sid
            ctype = (resp.headers.get("Content-Type") or "").lower()
            body = resp.read().decode("utf-8", "replace")
        if "text/event-stream" in ctype:
            for line in body.splitlines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                try:
                    msg = json.loads(line[5:].strip())
                except ValueError:
                    continue
                if isinstance(msg, dict) and ("result" in msg or "error" in msg):
                    return msg
            raise McpError("MCP SSE stream carried no JSON-RPC answer")
        if not body.strip():
            return {}
        try:
            return json.loads(body)
        except ValueError:
            raise McpError("MCP server returned a non-JSON answer") from None

    def request(self, method, params=None, timeout=CALL_TIMEOUT):
        with self._lock:
            self._next_id += 1
            rid = self._next_id
        msg = self._post({"jsonrpc": "2.0", "id": rid, "method": method,
                          "params": params or {}}, timeout)
        if not msg:
            return {}
        if "error" in msg:
            raise McpError(str(msg["error"].get("message", msg["error"])))
        return msg.get("result") or {}

    def notify(self, method, params=None):
        self._post({"jsonrpc": "2.0", "method": method, "params": params or {}},
                   CONNECT_TIMEOUT)

    def close(self):
        self.session = None


# ----------------------------------------------------------------- server
class McpServer:
    """One configured server: lazy connect, cached tool list."""

    def __init__(self, config):
        self.config = dict(config)
        self.name = sanitize(config.get("name") or "server")
        self.client = None
        self.tools = []
        self.error = ""
        self._lock = threading.Lock()

    def _make_client(self):
        cfg = self.config
        if (cfg.get("transport") or "stdio") == "http":
            if not cfg.get("url"):
                raise McpError("HTTP server needs a url")
            return HttpClient(cfg["url"], cfg.get("headers"))
        command = (cfg.get("command") or "").strip()
        if not command:
            raise McpError("stdio server needs a command")
        return StdioClient(command, cfg.get("args") or [], cfg.get("env"), cfg.get("cwd"))

    def ensure(self):
        with self._lock:
            if self.client is not None:
                return
            client = self._make_client()
            client.start()
            client.request("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": CLIENT_INFO}, timeout=CONNECT_TIMEOUT)
            client.notify("notifications/initialized")
            tools, cursor = [], None
            while True:
                params = {"cursor": cursor} if cursor else {}
                page = client.request("tools/list", params, timeout=CONNECT_TIMEOUT)
                tools.extend(page.get("tools") or [])
                cursor = page.get("nextCursor")
                if not cursor:
                    break
            self.tools = tools
            self.client = client
            self.error = ""

    def call(self, tool, arguments):
        self.ensure()
        return self.client.request("tools/call",
                                   {"name": tool, "arguments": arguments or {}})

    def close(self):
        with self._lock:
            client, self.client = self.client, None
            self.tools = []
        if client is not None:
            client.close()


def result_to_dict(result):
    """MCP tools/call result -> board-tool-style dict (errors as data)."""
    texts, image = [], None
    for block in result.get("content") or []:
        kind = block.get("type")
        if kind == "text":
            texts.append(block.get("text", ""))
        elif kind == "image" and image is None:
            image = {"data": block.get("data", ""),
                     "mime": block.get("mimeType", "image/png")}
        elif kind == "resource":
            resource = block.get("resource") or {}
            texts.append(resource.get("text") or json.dumps(resource, ensure_ascii=False))
    out = {"ok": not result.get("isError", False), "text": "\n".join(texts)}
    if image:
        out["_image"] = image
    if not out["ok"]:
        out["error"] = out["text"] or "MCP tool reported an error"
    elif not out["text"]:
        out["text"] = "(no output)"
    return out


# -------------------------------------------------------------------- hub
class McpHub:
    """All enabled servers; public names are mcp__<server>__<tool>."""

    def __init__(self, configs=()):
        self.servers = {}
        for cfg in configs or []:
            if not cfg.get("enabled", True):
                continue
            server = McpServer(cfg)
            name, n = server.name, 2
            while name in self.servers:
                name = "%s_%d" % (server.name, n)
                n += 1
            server.name = name
            self.servers[name] = server
        self._routes = {}
        self._lock = threading.Lock()

    def schemas(self):
        out = []
        for server in self.servers.values():
            try:
                server.ensure()
            except Exception as ex:
                server.error = str(ex) if isinstance(ex, McpError) else "%s: %s" % (type(ex).__name__, ex)
                continue
            for tool in server.tools:
                public = (NAME_PREFIX + server.name + "__" +
                          sanitize(tool.get("name", "")))[:64]
                with self._lock:
                    self._routes[public] = (server, tool.get("name", ""))
                out.append({"name": public,
                            "description": ("[%s] %s" % (server.name,
                                                         tool.get("description", "")))[:1024],
                            "parameters": tool.get("inputSchema") or
                                          {"type": "object", "properties": {}}})
        return out

    def names(self):
        with self._lock:
            return list(self._routes)

    def errors(self):
        return {name: s.error for name, s in self.servers.items() if s.error}

    def is_dangerous(self, name):
        """MCP calls need per-call confirmation unless the server is trusted."""
        with self._lock:
            route = self._routes.get(name)
        if route is None:
            return True
        return bool(route[0].config.get("confirm", True))

    def call(self, name, arguments=None):
        with self._lock:
            route = self._routes.get(name)
        if route is None:
            return {"ok": False, "error": "Unknown MCP tool '%s'" % name}
        server, original = route
        try:
            return result_to_dict(server.call(original, arguments))
        except McpError as ex:
            return {"ok": False, "error": str(ex)}
        except Exception as ex:
            return {"ok": False, "error": "%s: %s" % (type(ex).__name__, ex)}

    def close(self):
        for server in self.servers.values():
            server.close()
        with self._lock:
            self._routes.clear()


class ToolSet:
    """BoardTools + MCP behind the exact interface the Agent expects."""

    def __init__(self, board, hub=None):
        self.board = board
        self.hub = hub

    def schemas(self):
        out = self.board.schemas()
        if self.hub is not None:
            out = out + self.hub.schemas()
        return out

    def names(self):
        out = self.board.names()
        if self.hub is not None:
            out = out + self.hub.names()
        return out

    def is_dangerous(self, name):
        if self.hub is not None and name.startswith(NAME_PREFIX):
            return self.hub.is_dangerous(name)
        return self.board.is_dangerous(name)

    def mutates(self, name):
        return self.board.mutates(name)

    def call(self, name, arguments=None):
        if self.hub is not None and name.startswith(NAME_PREFIX):
            return self.hub.call(name, arguments)
        return self.board.call(name, arguments)


# -------------------------------------------------------------- self-test
_SERVER_SCRIPT = r'''
import json, sys
def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()
for line in sys.stdin:
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    method = msg.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {
            "protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
            "serverInfo": {"name": "demo", "version": "0"}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": [{
            "name": "echo", "description": "Echo text back",
            "inputSchema": {"type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"]}}]}})
    elif method == "tools/call":
        args = (msg.get("params") or {}).get("arguments") or {}
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {
            "content": [{"type": "text", "text": "echo: " + str(args.get("text", ""))}],
            "isError": False}})
    elif "id" in msg:
        send({"jsonrpc": "2.0", "id": msg["id"],
              "error": {"code": -32601, "message": "unknown method"}})
'''


def _selftest():
    """python -m refsynapse.ai.mcp_client  (demo stdio server, offline)"""
    hub = McpHub([{"name": "demo", "transport": "stdio",
                   "command": sys.executable, "args": ["-c", _SERVER_SCRIPT]}])
    schemas = hub.schemas()
    print("tools:", [s["name"] for s in schemas])
    print("errors:", hub.errors())
    if schemas:
        print("confirm required:", hub.is_dangerous(schemas[0]["name"]))
        print("call ->", hub.call(schemas[0]["name"], {"text": "привет MCP"}))
    hub.close()
    print("DONE")


def _serve():
    """python -m refsynapse.ai.mcp_client --serve  (demo stdio server)"""
    exec(_SERVER_SCRIPT)


if __name__ == "__main__":
    if "--serve" in sys.argv:
        _serve()
    else:
        _selftest()