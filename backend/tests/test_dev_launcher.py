"""本地开发启动器环境契约测试。"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

PROXY_ENV_NAMES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def test_dev_shell_launcher_does_not_pass_proxy_environment_to_children(tmp_path):
    root = Path(__file__).resolve().parents[2]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    checker = """#!/usr/bin/env bash
set -eu
for name in HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy; do
  if [[ -n "${!name-}" ]]; then
    echo "proxy leaked: $name" >&2
    exit 41
  fi
done
if [[ "${NO_PROXY-}" != "127.0.0.1,localhost,::1" ]]; then
  echo "unexpected NO_PROXY: ${NO_PROXY-}" >&2
  exit 42
fi
if [[ "${no_proxy-}" != "127.0.0.1,localhost,::1" ]]; then
  echo "unexpected no_proxy: ${no_proxy-}" >&2
  exit 43
fi
if [[ "${0##*/}" == "pnpm" ]]; then
  echo "frontend backend port:${TICKFLOW_BACKEND_PORT-}"
fi
echo "proxy env clean:${0##*/}"
sleep 0.1
"""
    _write_executable(fake_bin / "uv", checker)
    _write_executable(fake_bin / "pnpm", checker)
    _write_executable(fake_bin / "lsof", "#!/usr/bin/env bash\nexit 0\n")

    env = os.environ.copy()
    env.update({name: "http://proxy.invalid:7890" for name in PROXY_ENV_NAMES})
    env.update(
        {
            "NO_PROXY": "inherited.invalid",
            "no_proxy": "inherited.invalid",
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "BACKEND_PORT": "49181",
            "FRONTEND_PORT": "49182",
        }
    )

    result = subprocess.run(
        ["bash", str(root / "dev.sh")],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "proxy env clean:uv" in output
    assert "proxy env clean:pnpm" in output
    assert "frontend backend port:49181" in output
    assert "proxy leaked:" not in output


def test_dev_powershell_launcher_clears_proxy_in_parent_and_jobs():
    root = Path(__file__).resolve().parents[2]
    script = (root / "dev.ps1").read_text(encoding="utf-8")

    for name in PROXY_ENV_NAMES:
        assert f"'{name}'" in script
    assert script.count('Remove-Item -Path "Env:$name"') == 3
    assert script.count("$env:NO_PROXY = '127.0.0.1,localhost,::1'") == 3
    assert script.count("$env:no_proxy = $env:NO_PROXY") == 3
    assert script.count("$proxyEnvironmentVariableNames.Split(',')") == 2
    assert script.count("$ProxyEnvironmentVariableNames") == 3
    assert '$env:TICKFLOW_BACKEND_PORT = [string]$backendPort' in script
    assert (
        '$frontendPidFile, $FrontendDir, $FrontendPort, $BackendPort, '
        '$ProxyEnvironmentVariableNames'
    ) in script


def test_vite_proxy_uses_launcher_backend_port():
    root = Path(__file__).resolve().parents[2]
    config = (root / "frontend" / "vite.config.ts").read_text(encoding="utf-8")

    assert "process.env.TICKFLOW_BACKEND_PORT ?? '3018'" in config
    assert "target: backendTarget" in config
    assert "'/health': backendTarget" in config
