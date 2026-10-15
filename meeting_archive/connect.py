"""Install Yarn's local search server into desktop chat apps.

Ollama writes summaries. Search is a local Chroma index that an MCP client
starts with bin/chroma-mcp. Claude Desktop reads a JSON file, and so does the
third-party profile Ollama uses when Claude is pointed at a local model.
Ollama itself is not an MCP client, so Connect Ollama opens a local chat
that uses the same server.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from .config import Config

SERVER_NAME = "yarn"


class ConnectError(RuntimeError):
    pass


def server_spec(cfg: Config) -> dict:
    launcher = Path(__file__).resolve().parents[1] / "bin" / "chroma-mcp"
    return {
        "command": str(launcher),
        "args": [
            "--client-type",
            "persistent",
            "--data-dir",
            str(cfg.chroma_dir),
        ],
        "env": {"ANONYMIZED_TELEMETRY": "false"},
    }


def config_path(target: str) -> Path:
    home = Path.home()
    if target == "claude":
        return home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if target == "cursor":
        return home / ".cursor" / "mcp.json"
    raise ValueError(f"unknown connect target {target}")


def claude_config_paths() -> list[Path]:
    """Both Claude profiles that can launch a local server.

    The normal file is what Claude reads on its own. Once Ollama connects
    Claude, the app switches to the Claude-3p profile and never looks at the
    first file. A server written only to one of them looks connected and is
    not.
    """
    home = Path.home() / "Library" / "Application Support"
    return [
        home / "Claude" / "claude_desktop_config.json",
        home / "Claude-3p" / "claude_desktop_config.json",
    ]


def servers_file(cfg: Config) -> Path:
    return cfg.root / "mcp" / "yarn.json"


def merge_mcp_config(path: Path, spec: dict, *, name: str = SERVER_NAME) -> Path:
    """Add Yarn's server without dropping the rest of an existing config."""
    data: dict = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            loaded = {}
        if isinstance(loaded, dict):
            data = loaded
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    servers[name] = spec
    data["mcpServers"] = servers
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def install_search_server(cfg: Config) -> list[Path]:
    """Write Yarn's server into both Claude profiles and the Ollama client file."""
    spec = server_spec(cfg)
    written = [merge_mcp_config(path, spec) for path in claude_config_paths()]
    written.append(merge_mcp_config(servers_file(cfg), spec))
    return written


def connect_client(cfg: Config, target: str) -> Path:
    return merge_mcp_config(config_path(target), server_spec(cfg))


def connect_instructions(cfg: Config) -> str:
    spec = server_spec(cfg)
    args = " ".join(spec["args"])
    return (
        "ChatGPT does not read a config file Yarn can write. "
        "In the ChatGPT app open Settings, then Connectors, and add a local server. "
        f"Command: {spec['command']} {args}."
    )


def restart_claude(*, quit_first: bool, reopen: bool) -> None:
    """Quit before writing, then open after. Claude saves its config on quit.

    A write while Claude is open is undone the moment it exits, which is why
    the menu could report success and the next launch still had no server.
    """
    if quit_first:
        subprocess.run(
            ["/usr/bin/osascript", "-e", 'tell application "Claude" to quit'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        deadline = time.time() + 8
        while time.time() < deadline:
            running = subprocess.run(
                ["/usr/bin/pgrep", "-x", "Claude"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if running.returncode != 0:
                break
            time.sleep(0.3)
    if reopen:
        opened = subprocess.run(
            ["/usr/bin/open", "-a", "Claude"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
            text=True,
        )
        if opened.returncode != 0:
            detail = (opened.stderr or "").strip()
            raise ConnectError(
                "Claude isn't installed, so Yarn couldn't open it."
                + (f" {detail}" if detail else "")
            )


def probe_server(cfg: Config) -> str | None:
    """Start the search server briefly. None means it stayed up."""
    spec = server_spec(cfg)
    launcher = Path(spec["command"])
    if not launcher.is_file():
        return f"search launcher is missing at {launcher}"
    env = os.environ.copy()
    env.update(spec["env"])
    try:
        proc = subprocess.Popen(
            [spec["command"], *spec["args"]],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
    except OSError as exc:
        return str(exc)
    try:
        _, err = proc.communicate(timeout=8)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        return None
    if proc.returncode == 0:
        return None
    text = (err or b"").decode(errors="replace").strip()
    return text[-400:] or f"search server exited {proc.returncode}"


def ollama_problem(cfg: Config) -> str | None:
    """None when the configured local model answers on the Ollama host."""
    host = cfg.summary.host.rstrip("/")
    model = cfg.summary.model
    try:
        with urllib.request.urlopen(f"{host}/api/tags", timeout=2) as resp:
            body = json.loads(resp.read().decode())
    except (OSError, urllib.error.URLError, ValueError, TimeoutError):
        opened = subprocess.run(
            ["/usr/bin/open", "-a", "Ollama"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if opened.returncode != 0:
            return "Ollama isn't running. Open it, then click Connect Ollama again."
        deadline = time.time() + 8
        while time.time() < deadline:
            time.sleep(0.5)
            try:
                with urllib.request.urlopen(f"{host}/api/tags", timeout=2) as resp:
                    body = json.loads(resp.read().decode())
                break
            except (OSError, urllib.error.URLError, ValueError, TimeoutError):
                body = None
        if body is None:
            return "Ollama isn't running. Open it, then click Connect Ollama again."
    names = [str(item.get("name", "")) for item in body.get("models", []) if isinstance(item, dict)]
    if not any(name == model or name.startswith(model + ":") for name in names):
        return (
            f"Ollama doesn't have {model} yet. "
            "Pick Fast or Accurate in the Yarn menu after the download finishes."
        )
    return None


def open_ollama_chat(script: Path, model: str, servers: Path) -> None:
    """Open a Terminal window whose local model can call Yarn's server."""
    if not script.is_file():
        raise ConnectError(f"Ollama chat launcher is missing at {script}")
    opened = subprocess.run(
        [
            "/usr/bin/osascript",
            "-e",
            "on run argv",
            "-e",
            "set cmd to quoted form of (item 1 of argv) "
            '& " " & quoted form of (item 2 of argv) '
            '& " " & quoted form of (item 3 of argv)',
            "-e",
            'tell application "Terminal" to do script cmd',
            "-e",
            'tell application "Terminal" to activate',
            "-e",
            "end run",
            "--",
            str(script),
            model,
            str(servers),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
        text=True,
    )
    if opened.returncode != 0:
        detail = (opened.stderr or "").strip()
        raise ConnectError(
            "Yarn couldn't open the Ollama chat."
            + (f" {detail}" if detail else "")
        )


def _ollama_script() -> Path:
    return Path(__file__).resolve().parents[1] / "bin" / "yarn-ollama"


def connect_target(cfg: Config, target: str, *, notify=None) -> str:
    if target == "chatgpt":
        return connect_instructions(cfg)
    if target == "cursor":
        path = connect_client(cfg, target)
        return f"Cursor can search Yarn. Reload the window if it is already open. ({path})"
    if target == "claude":
        if notify:
            notify("Closing Claude so Yarn can connect, then reopening it.")
        restart_claude(quit_first=True, reopen=False)
        install_search_server(cfg)
        problem = probe_server(cfg)
        if problem:
            raise ConnectError(f"Yarn's search server did not start. {problem}")
        restart_claude(quit_first=False, reopen=True)
        return (
            "Claude is open with Yarn search. Start a new chat, click +, "
            "and turn on Yarn under Connectors."
        )
    if target == "ollama":
        problem = ollama_problem(cfg)
        if problem:
            raise ConnectError(problem)
        written = install_search_server(cfg)
        servers = next(path for path in written if path.name == "yarn.json")
        model = cfg.summary.model
        open_ollama_chat(_ollama_script(), model, servers)
        return (
            f"Ollama is opening in Terminal on {model}, with Yarn search connected. "
            "Ask it about a meeting."
        )
    raise ConnectError(f"unknown connect target {target}")
