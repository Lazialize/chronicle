"""Real containers + trusted local TLS + MCP, with no LLM/API requests.

Run: python3 tests/docker_smoke.py
Uses a unique Compose project and deletes only that project's test volumes.
"""

import http.client
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
import subprocess
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]


def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def main():
    token = secrets.token_hex(32)
    domain = "chronicle.specite.dev"
    env = dict(os.environ, MCP_DOMAIN=domain, ACME_EMAIL="test@example.com",
               MCP_BEARER_TOKEN=token, OPENAI_API_KEY="local-smoke-test-not-a-real-key",
               MODEL_NAME="gpt-4.1-mini", EMBEDDER_MODEL="text-embedding-3-small",
               GRAPHITI_GROUP_ID="main", SEMAPHORE_LIMIT="2")
    config = json.loads(run(
        "docker", "compose", "--env-file", str(ROOT / ".env.example"),
        "-f", str(ROOT / "compose.yaml"), "config", "--format", "json",
        env=env, capture_output=True,
    ).stdout)
    project = "chronicle-smoke-" + secrets.token_hex(4)
    config["name"] = project
    # No production ports, volumes, API keys, DNS, or public ACME requests.
    config["services"]["caddy"]["ports"] = ["127.0.0.1::8443"]
    config["networks"]["outbound"]["internal"] = True
    for section in ("networks", "volumes"):
        for name, settings in config[section].items():
            settings["name"] = f"{project}_{name}"
    with tempfile.TemporaryDirectory(prefix="chronicle-smoke-") as temp:
        directory = Path(temp)
        # Use the production Host/Origin, but a private test CA and test listener.
        caddyfile = directory / "Caddyfile"
        caddyfile.write_text((ROOT / "Caddyfile").read_text().replace(
            "{$MCP_DOMAIN} {", "{$MCP_DOMAIN}:8443 {\n\ttls internal"))
        for mount in config["services"]["caddy"]["volumes"]:
            if mount["target"] == "/etc/caddy/Caddyfile":
                mount["source"] = str(caddyfile)
        compose_file = directory / "compose.json"
        compose_file.write_text(json.dumps(config))
        compose_file.chmod(0o600)
        base = ["docker", "compose", "-p", project, "-f", str(compose_file)]

        def compose(*args, **kwargs):
            return run(*base, *args, **kwargs)

        try:
            print("Starting isolated Graphiti, FalkorDB and Caddy containers…", flush=True)
            compose("up", "-d", "--quiet-pull", "--wait", "--wait-timeout", "240")
            address = compose("port", "caddy", "8443", capture_output=True).stdout.strip()
            port = int(address.rsplit(":", 1)[1])
            ca = directory / "root.crt"
            compose("cp", "caddy:/data/caddy/pki/authorities/local/root.crt", str(ca))
            context = ssl.create_default_context(cafile=str(ca))
            session = None
            protocol = None

            def request(payload=None, authorization=token, origin=None, method="POST"):
                headers = {"Host": domain, "Accept": "application/json, text/event-stream",
                           "Content-Type": "application/json"}
                if authorization is not None:
                    headers["Authorization"] = "Bearer " + authorization
                if origin is not None:
                    headers["Origin"] = origin
                if session:
                    headers["Mcp-Session-Id"] = session
                if protocol:
                    headers["MCP-Protocol-Version"] = protocol
                conn = http.client.HTTPSConnection(domain, context=context, timeout=20)
                try:
                    conn.sock = context.wrap_socket(
                        socket.create_connection(("127.0.0.1", port), timeout=20),
                        server_hostname=domain,
                    )
                    conn.request(method, "/mcp/", json.dumps(payload) if payload else None, headers)
                    response = conn.getresponse()
                    response_headers = dict((k.lower(), v) for k, v in response.getheaders())
                    if response.status == 200 and "text/event-stream" in response.getheader("Content-Type", ""):
                        # A Streamable HTTP response may keep the connection open.
                        while True:
                            line = response.readline()
                            if not line:
                                raise AssertionError("SSE stream ended before an RPC result")
                            if line.startswith(b"data:"):
                                value = json.loads(line[5:])
                                if value.get("id") == payload.get("id"):
                                    return response.status, response_headers, value
                    body = response.read()
                    try:
                        body = json.loads(body)
                    except (ValueError, UnicodeDecodeError):
                        body = body.decode()
                    return response.status, response_headers, body
                finally:
                    conn.close()

            def rpc(method, params, request_id):
                status, headers, body = request({"jsonrpc": "2.0", "id": request_id,
                                                 "method": method, "params": params},
                                                origin=f"https://{domain}")
                assert status == 200, (status, body)
                assert "error" not in body, body
                return headers, body["result"]

            for method in ("GET", "POST", "DELETE"):
                for credential in (None, "wrong"):
                    assert request(authorization=credential, method=method)[0] == 401
            assert request(origin="https://evil.example")[0] == 403
            print("PASS: TLS verified; missing/wrong Bearer and invalid Origin rejected", flush=True)
            headers, result = rpc("initialize", {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "chronicle-smoke", "version": "1.0"},
            }, 1)
            session = headers.get("mcp-session-id")
            protocol = result["protocolVersion"]
            status, _, _ = request({"jsonrpc": "2.0", "method": "notifications/initialized"})
            assert status == 202, status
            _, result = rpc("tools/list", {}, 2)
            names = {tool["name"] for tool in result["tools"]}
            assert {"add_memory", "search_nodes", "get_status"}.issubset(names), names
            _, result = rpc("tools/call", {"name": "get_status", "arguments": {}}, 3)
            assert not result.get("isError"), result
            status_result = result.get("structuredContent") or json.loads(next(
                item["text"] for item in result["content"] if item["type"] == "text"))
            assert status_result["status"] == "ok", status_result
            print(f"PASS: MCP initialize, tools/list ({len(names)} tools), get_status", flush=True)

            # Write only a synthetic DB graph; do not call LLM-dependent add_memory.
            persistence = compose("exec", "-T", "falkordb", "redis-cli", "INFO", "persistence",
                                  capture_output=True).stdout
            assert "aof_enabled:1" in persistence, persistence
            compose("exec", "-T", "falkordb", "redis-cli", "GRAPH.QUERY", "chronicle_smoke",
                    "CREATE (:Smoke {value: 'persisted'})", capture_output=True)
            compose("restart", "falkordb")
            for _ in range(60):
                try:
                    output = compose("exec", "-T", "falkordb", "redis-cli", "GRAPH.QUERY",
                                     "chronicle_smoke", "MATCH (n:Smoke) RETURN n.value",
                                     capture_output=True).stdout
                    if "persisted" in output:
                        break
                except subprocess.CalledProcessError:
                    pass
                time.sleep(1)
            else:
                raise AssertionError("Graph did not persist through DB restart")
            print("PASS: FalkorDB graph persisted through restart", flush=True)
        except Exception:
            compose("logs", "--tail", "60")
            raise
        finally:
            compose("down", "--volumes", "--remove-orphans", "--timeout", "10")


if __name__ == "__main__":
    main()
