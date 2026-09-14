"""Codex CLI 文本生成模式的本地工具隔离集成测试。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

from app.services import ai_provider


class _CaptureHandler(BaseHTTPRequestHandler):
    requests: ClassVar[list[dict]] = []

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        type(self).requests.append(json.loads(body))
        payload = json.dumps({"error": {"message": "captured"}}).encode()
        self.send_response(400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format: str, *args: object) -> None:
        del args


@pytest.mark.skipif(shutil.which("codex") is None, reason="未安装 Codex CLI")
def test_codex_exec_request_disables_command_and_network_tools(tmp_path: Path) -> None:
    """检查命令及网络工具关闭；文件写权限另用实际工具调用验证。"""
    _CaptureHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CaptureHandler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        codex_home = tmp_path / "codex-home"
        workspace = tmp_path / "workspace"
        codex_home.mkdir()
        workspace.mkdir()
        port = server.server_address[1]
        (codex_home / "config.toml").write_text(
            "\n".join([
                'model_provider = "capture"',
                'model = "gpt-5.5"',
                'approval_policy = "never"',
                'sandbox_mode = "read-only"',
                "",
                "[model_providers.capture]",
                'name = "Capture"',
                f'base_url = "http://127.0.0.1:{port}/v1"',
                'wire_api = "responses"',
                "requires_openai_auth = false",
                "",
            ]),
            encoding="utf-8",
        )
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(tmp_path),
            "CODEX_HOME": str(codex_home),
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        }
        args = [
            shutil.which("codex") or "codex",
            "exec",
            "--strict-config",
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--json",
            "-c",
            'web_search="disabled"',
        ]
        disabled = ai_provider._supported_codex_features(
            [args[0]],
            env,
            ai_provider._CODEX_DISABLED_LOCAL_FEATURES,
        )
        for feature in disabled:
            args.extend(["--disable", feature])
        args.extend(["--cd", str(workspace), "仅回复 ok"])

        completed = subprocess.run(
            args,
            capture_output=True,
            env=env,
            timeout=30,
            check=False,
        )
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)

    assert _CaptureHandler.requests, completed.stderr.decode(errors="replace")
    request = _CaptureHandler.requests[-1]
    tools = request.get("tools") or []
    names = [tool.get("name") or tool.get("type") for tool in tools]
    forbidden = {
        "shell", "shell_command", "exec_command", "local_shell",
        "web_search", "web_search_preview", "browser", "computer",
        "mcp", "read_mcp_resource", "list_mcp_resources",
    }
    assert not forbidden.intersection(names), f"Codex CLI 仍暴露命令或网络工具: {names!r}"


@pytest.mark.skipif(shutil.which("codex") is None, reason="未安装 Codex CLI")
@pytest.mark.parametrize("outside_workspace", [False, True])
def test_codex_readonly_rejects_actual_patch(tmp_path, outside_workspace):
    """仅向本机假服务发送合成数据，实际触发工具并验证只读边界。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_dir = tmp_path / "isolated-config"
    config_dir.mkdir()
    target = (tmp_path if outside_workspace else workspace) / "canary.txt"
    target.write_text("synthetic-before\n", encoding="utf-8")
    requests = []

    class Handler(_CaptureHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(request)
            if len(requests) == 1:
                item = {
                    "type": "custom_tool_call", "id": "tool_canary",
                    "call_id": "call_canary", "name": "apply_patch",
                    "input": f"*** Begin Patch\n*** Update File: {target}\n@@\n-synthetic-before\n+synthetic-after\n*** End Patch",
                }
            else:
                item = {
                    "type": "message", "id": "msg_done", "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "ok", "annotations": []}],
                }
            response = {"id": f"resp_{len(requests)}", "object": "response", "status": "completed", "output": [item]}
            events = [
                {"type": "response.output_item.done", "output_index": 0, "item": item},
                {"type": "response.completed", "response": response},
            ]
            payload = "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        (config_dir / "config.toml").write_text(
            'model_provider="capture"\nmodel="gpt-5.5"\napproval_policy="never"\n'
            'sandbox_mode="read-only"\n[model_providers.capture]\nname="Capture"\n'
            f'base_url="http://127.0.0.1:{server.server_port}/v1"\n'
            'wire_api="responses"\nrequires_openai_auth=false\n', encoding="utf-8",
        )
        env = {
            "PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path),
            "CODEX_HOME": str(config_dir), "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        }
        binary = shutil.which("codex")
        args = [binary, "exec", "--strict-config", "--sandbox", "read-only", "--skip-git-repo-check", "--json", "-c", 'web_search="disabled"']
        for feature in ai_provider._supported_codex_features([binary], env, ai_provider._CODEX_DISABLED_LOCAL_FEATURES):
            args.extend(["--disable", feature])
        args.extend(["--cd", str(workspace), "仅测试合成文件的只读边界。"])
        completed = subprocess.run(args, env=env, capture_output=True, timeout=30, check=False)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    assert len(requests) >= 2, "必须实际执行工具并向模型返回结果，不能只验证参数"
    outputs = [item for item in requests[-1].get("input", []) if item.get("call_id") == "call_canary" and item.get("type", "").endswith("_output")]
    assert outputs, "未捕获工具执行结果"
    assert target.read_text(encoding="utf-8") == "synthetic-before\n"
    assert any(word in json.dumps(outputs).lower() for word in ("reject", "denied", "not permitted", "read-only"))
