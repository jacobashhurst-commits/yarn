"""Install Yarn on this Mac without asking anyone to touch Python.

`python3 -m meeting_archive.setup` is the worker the Setup window reads.
Each step prints one JSON line so the window can show 3 of 9, a tick, and
the current line of detail. Re-running is safe: finished work is skipped.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

WHISPER_GGML = (
    "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/"
    "ggml-large-v3-turbo.bin"
)
DIARIZE_SEG = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    "speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
)
DIARIZE_EMB = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    "speaker-recongition-models/"
    "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx"
)
FFMPEG_STATIC = (
    "https://github.com/eugeneware/ffmpeg-static/releases/download/"
    "b6.1.1/ffmpeg-darwin-arm64"
)
OLLAMA_ZIP = "https://ollama.com/download/Ollama-darwin.zip"
SWIFTBAR_ZIP = (
    "https://github.com/swiftbar/SwiftBar/releases/download/"
    "v2.1.1/SwiftBar.v2.1.1.b597.zip"
)
MIN_FREE_BYTES = 10 * 1024 ** 3

STEPS: list[tuple[str, str]] = [
    ("prereqs", "System prerequisites"),
    ("venv", "Create Python environment"),
    ("deps", "Install Python dependencies"),
    ("tools", "Install local tools"),
    ("config", "Prepare config"),
    ("whisper", "Download speech model"),
    ("speakers", "Download speaker model"),
    ("ollama", "Set up Ollama"),
    ("menubar", "Install menu bar and background jobs"),
]


def repo_dir() -> Path:
    return Path(__file__).resolve().parents[1]


def runtime_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / "Yarn" / "app"


# macOS Files-and-Folders TCC. Terminal can read these; a LaunchAgent cannot.
# The recorder then dies on `.venv/pyvenv.cfg` with Operation not permitted.
TCC_BLOCKED_FOLDERS = ("Downloads", "Desktop", "Documents")


def is_blocked_for_launchd(path: Path) -> bool:
    resolved = path.expanduser().resolve()
    try:
        relative = resolved.relative_to(Path.home().resolve())
    except ValueError:
        return False
    if relative.parts and relative.parts[0] in TCC_BLOCKED_FOLDERS:
        return True
    return "Mobile Documents" in str(relative)


def copy_checkout(source: Path, dest: Path) -> None:
    if dest.exists() and dest.resolve() == source.resolve():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        source,
        dest,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(
            ".venv", "__pycache__", "*.pyc", ".pytest_cache", ".git"
        ),
    )


def materialize_runtime(source: Path) -> Path:
    """Yarn's recorder is a LaunchAgent. It must not live in Downloads."""
    source = source.expanduser().resolve()
    if not is_blocked_for_launchd(source):
        emit({"type": "log", "id": "venv", "text": f"Installing in place at {source}"})
        return source
    dest = runtime_dir()
    folder = source.relative_to(Path.home().resolve()).parts[0]
    emit(
        {
            "type": "log",
            "id": "venv",
            "text": (
                f"macOS blocks background jobs in {folder}; "
                f"copying Yarn to {dest}"
            ),
        }
    )
    copy_checkout(source, dest)
    run(["xattr", "-cr", str(dest)], timeout=60)
    return dest


def venv_python(repo: Path) -> Path:
    return repo / ".venv" / "bin" / "python"


def support_bin() -> Path:
    path = Path.home() / "Library" / "Application Support" / "Yarn" / "bin"
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_dir() -> Path:
    path = Path.home() / "Library" / "Caches" / "Yarn"
    path.mkdir(parents=True, exist_ok=True)
    return path


def existing_app(name: str) -> Path | None:
    for folder in (Path("/Applications"), Path.home() / "Applications"):
        path = folder / name
        if path.exists():
            return path
    return None


def install_app_from_zip(url: str, app_name: str, step_id: str) -> Path:
    """Put a .app from a zip into Applications. No Homebrew required."""
    found = existing_app(app_name)
    if found:
        return found
    archive = cache_dir() / app_name.replace(" ", "") / "download.zip"
    extract = cache_dir() / app_name.replace(" ", "") / "extract"
    if extract.exists():
        shutil.rmtree(extract)
    extract.mkdir(parents=True)
    download(url, archive, step_id)
    unzip = run(["unzip", "-q", str(archive), "-d", str(extract)], timeout=120)
    if unzip.returncode != 0:
        raise RuntimeError(f"Could not unpack {app_name}: {(unzip.stderr or '')[-400:]}")
    apps = [p for p in extract.rglob("*.app") if p.name == app_name]
    if not apps:
        apps = list(extract.rglob("*.app"))
    if not apps:
        raise RuntimeError(f"The {app_name} download did not contain an app.")
    src = apps[0]
    last_error = ""
    for folder in (Path("/Applications"), Path.home() / "Applications"):
        dest = folder / app_name
        try:
            folder.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                return dest
            shutil.copytree(src, dest)
            run(["xattr", "-cr", str(dest)], timeout=30)
            emit({"type": "log", "id": step_id, "text": f"Installed {app_name} to {dest}"})
            return dest
        except OSError as exc:
            last_error = str(exc)
            continue
    raise RuntimeError(f"Could not install {app_name}: {last_error}")


def ollama_binary() -> str | None:
    for path in (
        which("ollama"),
        "/usr/local/bin/ollama",
        "/opt/homebrew/bin/ollama",
        "/Applications/Ollama.app/Contents/Resources/ollama",
        str(Path.home() / "Applications/Ollama.app/Contents/Resources/ollama"),
    ):
        if path and Path(path).is_file():
            return path
    return None


def emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def which(name: str) -> str | None:
    return shutil.which(name)


def run(
    argv: list[str],
    *,
    env: dict | None = None,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    merged.setdefault("HOMEBREW_NO_AUTO_UPDATE", "1")
    merged.setdefault("PIP_DISABLE_PIP_VERSION_CHECK", "1")
    return subprocess.run(
        argv,
        text=True,
        capture_output=True,
        env=merged,
        timeout=timeout,
    )


def download(url: str, dest: Path, step_id: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_suffix(dest.suffix + ".partial")
    req = urllib.request.Request(url, headers={"User-Agent": "YarnSetup/0.2"})
    emit({"type": "log", "id": step_id, "text": f"Downloading {dest.name}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        got = 0
        last = -1
        with partial.open("wb") as fh:
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
                got += len(chunk)
                if total:
                    percent = min(100, int(got * 100 / total))
                    if percent != last:
                        last = percent
                        emit(
                            {
                                "type": "progress",
                                "id": step_id,
                                "percent": percent,
                            }
                        )
    partial.replace(dest)


def host_python() -> str:
    for name in ("python3.12", "python3.13", "python3.11", "python3"):
        path = which(name)
        if path:
            return path
    raise RuntimeError("Python 3.11+ was not found on this Mac.")


def step_prereqs(repo: Path) -> None:
    if platform.system() != "Darwin":
        raise RuntimeError("This installer is Mac only for now.")
    if platform.machine() != "arm64":
        raise RuntimeError(
            "Yarn Setup is Apple Silicon only. Intel Macs are not supported."
        )
    free = shutil.disk_usage(Path.home()).free
    gb = free / (1024 ** 3)
    emit({"type": "log", "id": "prereqs", "text": f"{gb:.0f} GB free on this disk"})
    if free < MIN_FREE_BYTES:
        raise RuntimeError(
            f"Need about 10 GB free for speech and summary models; {gb:.0f} GB left."
        )
    py = host_python()
    emit({"type": "log", "id": "prereqs", "text": f"Using {py}"})
    from .resources import summary_model_choice, unified_memory_gb

    ram_gb = unified_memory_gb()
    if ram_gb:
        emit({"type": "log", "id": "prereqs", "text": f"{ram_gb:.0f} GB of memory"})
    model, notice = summary_model_choice(ram_gb)
    if notice:
        emit({"type": "log", "id": "prereqs", "text": notice})
    else:
        emit(
            {
                "type": "log",
                "id": "prereqs",
                "text": f"Written summaries will use {model}",
            }
        )
    if not (repo / "pyproject.toml").is_file():
        raise RuntimeError(f"This does not look like a Yarn checkout: {repo}")


def step_venv(repo: Path) -> None:
    py = venv_python(repo)
    os.environ["PATH"] = f"{py.parent}:{os.environ.get('PATH', '')}"
    if py.is_file():
        emit({"type": "log", "id": "venv", "text": f"Already have {py}"})
        return
    host = host_python()
    emit({"type": "log", "id": "venv", "text": f"Creating {repo / '.venv'}"})
    uv = which("uv")
    if uv:
        proc = run([uv, "venv", "--python", host, str(repo / ".venv")])
    else:
        proc = run([host, "-m", "venv", str(repo / ".venv")])
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "Could not create a Python environment.")


def step_deps(repo: Path) -> None:
    py = venv_python(repo)
    emit({"type": "log", "id": "deps", "text": "Installing Yarn into the local environment"})
    uv = which("uv")
    lock = repo / "uv.lock"
    if uv and lock.is_file():
        # Bare `uv pip install -e` ignores the lock and once pulled PyAV 19,
        # which cannot open audio for faster-whisper's VAD.
        proc = run(
            [uv, "sync", "--frozen", "--directory", str(repo)],
            timeout=600,
        )
    elif uv:
        proc = run(
            [uv, "pip", "install", "--python", str(py), "-e", str(repo)],
            timeout=600,
        )
    else:
        pip = run([str(py), "-m", "pip", "--version"], timeout=30)
        if pip.returncode != 0:
            run([str(py), "-m", "ensurepip", "--upgrade"], timeout=120)
        proc = run([str(py), "-m", "pip", "install", "-e", str(repo)], timeout=600)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "install failed").strip()[-2000:])
    ma = repo / ".venv" / "bin" / "ma"
    if not ma.is_file():
        raise RuntimeError("Yarn installed but the `ma` command is missing.")
    emit({"type": "log", "id": "deps", "text": f"ma is at {ma}"})


def _brew_install(formula: str, cask: bool = False, step_id: str = "tools") -> bool:
    brew = which("brew")
    if not brew:
        return False
    argv = [brew, "install"]
    if cask:
        argv.append("--cask")
    argv.append(formula)
    emit({"type": "log", "id": step_id, "text": " ".join(argv)})
    proc = run(argv, timeout=900)
    return proc.returncode == 0


def step_tools(repo: Path) -> None:
    ffmpeg = which("ffmpeg")
    if not ffmpeg:
        if _brew_install("ffmpeg"):
            ffmpeg = which("ffmpeg")
    if not ffmpeg:
        dest = support_bin() / "ffmpeg"
        if not dest.is_file():
            download(FFMPEG_STATIC, dest, "tools")
            dest.chmod(0o755)
        ffmpeg = str(dest)
    os.environ["PATH"] = f"{support_bin()}:{os.environ.get('PATH', '')}"
    emit({"type": "log", "id": "tools", "text": f"ffmpeg: {ffmpeg}"})
    probe = run([ffmpeg, "-version"], timeout=20)
    if probe.returncode != 0:
        raise RuntimeError("ffmpeg is present but would not run.")

    whisper = which("whisper-cli")
    if not whisper and _brew_install("whisper-cpp"):
        whisper = which("whisper-cli")
    if whisper:
        emit({"type": "log", "id": "tools", "text": f"whisper.cpp: {whisper}"})
    else:
        emit(
            {
                "type": "log",
                "id": "tools",
                "text": "No whisper-cli; Yarn will use faster-whisper instead.",
            }
        )


def _gecos_name() -> str:
    import pwd

    gecos = (pwd.getpwuid(os.getuid()).pw_gecos or "").split(",")[0].strip()
    return gecos or os.environ.get("USER", "Me").title()


def _guess_mic() -> str:
    # ffmpeg prefixes every line with `[AVFoundation indev @ 0x…]`. Taking the
    # first `]` produced `1] MacBook Neo Microphone` and the recorder refused
    # to start. Use the same parser as `ma devices`.
    from .audio import list_avfoundation_devices, parse_audio_devices

    try:
        devices = parse_audio_devices(list_avfoundation_devices())
    except Exception:
        return ""
    for name in devices.values():
        lower = name.lower()
        if "macbook" in lower and "micro" in lower:
            return name
        if "built-in" in lower and "micro" in lower:
            return name
    return ""


def step_config(repo: Path) -> None:
    from .config import DEFAULT_CONFIG_PATH, DEFAULT_ROOT
    from .resources import summary_model_choice

    DEFAULT_ROOT.mkdir(parents=True, exist_ok=True)
    (DEFAULT_ROOT / "models" / "whisper-cpp").mkdir(parents=True, exist_ok=True)
    (DEFAULT_ROOT / "models" / "diarization").mkdir(parents=True, exist_ok=True)
    DEFAULT_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    summary_model, _ = summary_model_choice()
    if DEFAULT_CONFIG_PATH.exists():
        write_summary_model(DEFAULT_CONFIG_PATH, summary_model)
        emit(
            {
                "type": "log",
                "id": "config",
                "text": f"Keeping existing {DEFAULT_CONFIG_PATH} (summaries: {summary_model})",
            }
        )
        return
    whisper_cli = which("whisper-cli") or "/opt/homebrew/bin/whisper-cli"
    engine = "whisper_cpp" if which("whisper-cli") else "faster_whisper"
    model_path = DEFAULT_ROOT / "models" / "whisper-cpp" / "ggml-large-v3-turbo.bin"
    mic = _guess_mic()
    own = _gecos_name().split()[0].replace('"', "")
    mic = mic.replace('"', "")
    root = DEFAULT_ROOT.expanduser()
    body = f"""# Written by Yarn Setup. Room recording on the laptop microphone.
root = "{root}"
own_label = "{own}"
other_label = "Other"
room_label = "Unknown"
collection = "meetings"
audio_retention_days = 7

[audio]
device_name = "{mic}"
device_index = -1
mic_channels = [0]
sys_channels = [0]
sample_rate = 16000

[whisper]
model = "large-v3-turbo"
engine = "{engine}"
command = "{whisper_cli}"
model_path = "{model_path}"
num_threads = 8
device = "cpu"
compute_type = "int8"
local_files_only = true
language = "en"
beam_size = 5
vad_filter = true
vad_min_silence_ms = 700
condition_on_previous_text = false

[diarize]
enabled = true
engine = "pyannote"
segmentation_model = "{root}/models/diarization/sherpa-onnx-pyannote-segmentation-3-0/model.onnx"
embedding_model = "{root}/models/diarization/speaker-embedding.onnx"
identify_popup = true

[summary]
model = "{summary_model}"
when = "after_meeting"
"""
    DEFAULT_CONFIG_PATH.write_text(body, encoding="utf-8")
    emit(
        {
            "type": "log",
            "id": "config",
            "text": f"Wrote {DEFAULT_CONFIG_PATH}"
            + (f' (mic: "{mic}")' if mic else " (mic will be picked at first record)"),
        }
    )


def step_whisper(repo: Path) -> None:
    from .config import load_config

    cfg = load_config()
    if cfg.whisper.engine != "whisper_cpp":
        emit(
            {
                "type": "log",
                "id": "whisper",
                "text": "faster-whisper will fetch weights on first meeting.",
            }
        )
        return
    path = Path(cfg.whisper.model_path).expanduser()
    if path.is_file() and path.stat().st_size > 100_000_000:
        emit({"type": "log", "id": "whisper", "text": f"Already have {path.name}"})
        return
    download(WHISPER_GGML, path, "whisper")
    emit({"type": "log", "id": "whisper", "text": f"Saved {path}"})


def extract_segmentation_archive(archive: Path, dest_model: Path) -> None:
    """Unpack the sherpa tarball where diarize actually looks.

    The archive is a folder named `sherpa-onnx-pyannote-segmentation-3-0`.
    Extracting into that folder itself nests `model.onnx` one level too deep,
    which is how a fresh Setup left every room labelled Unknown.
    """
    import tarfile

    dest_model.parent.mkdir(parents=True, exist_ok=True)
    extract_root = dest_model.parent.parent
    extract_root.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:bz2") as tar:
        kwargs = {"filter": "data"} if sys.version_info >= (3, 12) else {}
        tar.extractall(extract_root, **kwargs)
    flatten_segmentation_model(dest_model)


def flatten_segmentation_model(dest_model: Path) -> None:
    """Undo a nested extract from an earlier Setup."""
    if dest_model.is_file():
        return
    nested = dest_model.parent / dest_model.parent.name / dest_model.name
    if not nested.is_file():
        return
    nested_dir = nested.parent
    for item in nested_dir.iterdir():
        target = dest_model.parent / item.name
        if target.exists():
            continue
        item.rename(target)


def step_speakers(repo: Path) -> None:
    from .config import load_config

    cfg = load_config()
    seg = Path(cfg.diarize.segmentation_model).expanduser()
    emb = Path(cfg.diarize.embedding_model).expanduser()
    flatten_segmentation_model(seg)
    if seg.is_file() and emb.is_file():
        emit({"type": "log", "id": "speakers", "text": "Speaker models already on disk"})
        return
    tmp = Path.home() / "Library" / "Caches" / "Yarn"
    tmp.mkdir(parents=True, exist_ok=True)
    archive = tmp / "segmentation.tar.bz2"
    if not seg.is_file():
        download(DIARIZE_SEG, archive, "speakers")
        extract_segmentation_archive(archive, seg)
        emit({"type": "log", "id": "speakers", "text": f"Segmentation model in {seg.parent}"})
    if not emb.is_file():
        download(DIARIZE_EMB, emb, "speakers")
    emit({"type": "log", "id": "speakers", "text": "Speaker models ready"})


def write_summary_model(path: Path, model: str) -> None:
    """Add or replace only the summary model. Never rewrite the rest of config."""
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if "[summary]" not in text:
        path.write_text(
            text.rstrip()
            + f'\n\n[summary]\nmodel = "{model}"\nwhen = "after_meeting"\n',
            encoding="utf-8",
        )
        return
    lines = text.splitlines()
    in_summary = False
    replaced = False
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_summary = stripped == "[summary]"
        if in_summary and stripped.startswith("model "):
            out.append(f'model = "{model}"')
            replaced = True
            continue
        out.append(line)
    if not replaced:
        patched: list[str] = []
        for line in out:
            patched.append(line)
            if line.strip() == "[summary]":
                patched.append(f'model = "{model}"')
                replaced = True
        out = patched
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def step_ollama(repo: Path) -> None:
    ollama = ollama_binary()
    if not ollama:
        if _brew_install("ollama", cask=True, step_id="ollama"):
            ollama = ollama_binary()
    if not ollama:
        emit({"type": "log", "id": "ollama", "text": "Downloading Ollama (free, stays on this Mac)"})
        try:
            app = install_app_from_zip(OLLAMA_ZIP, "Ollama.app", "ollama")
        except Exception as exc:
            emit(
                {
                    "type": "log",
                    "id": "ollama",
                    "text": f"Could not install Ollama ({exc}). Recording still works; summaries can wait.",
                }
            )
            return
        subprocess.Popen(["open", "-a", str(app)])
        for _ in range(20):
            time.sleep(2)
            ollama = ollama_binary()
            if ollama:
                break
    if not ollama:
        emit(
            {
                "type": "log",
                "id": "ollama",
                "text": "Ollama is installed but not ready yet. Open it once from the Yarn menu.",
            }
        )
        return
    from .config import DEFAULT_CONFIG_PATH, load_config, write_setting
    from .resources import (
        QUALITY_ACCURATE,
        can_run_accurate,
        model_for_quality,
        quality_for_model,
        summary_model_choice,
    )

    pulls = requested_pulls()
    listed = run([ollama, "list"], timeout=60)
    installed = listed.stdout or ""
    emit({"type": "log", "id": "ollama", "text": f"Using {ollama}"})
    if not can_run_accurate():
        emit(
            {
                "type": "log",
                "id": "ollama",
                "text": "This Mac can only run Fast summaries (8 GB). Accurate needs about 16 GB.",
            }
        )
    for quality in pulls:
        model = model_for_quality(quality)
        label = "Accurate" if quality == QUALITY_ACCURATE else "Fast"
        if model in installed:
            emit({"type": "log", "id": "ollama", "text": f"{label} ({model}) already installed"})
            continue
        emit({"type": "log", "id": "ollama", "text": f"Downloading {label} ({model})"})
        proc = run([ollama, "pull", model], timeout=3600)
        if proc.returncode != 0:
            emit(
                {
                    "type": "log",
                    "id": "ollama",
                    "text": f"Could not download {label}; summaries can wait.",
                }
            )
            continue
        installed += f"\n{model}"
        emit({"type": "log", "id": "ollama", "text": f"{label} ready"})
    default, _ = summary_model_choice()
    if QUALITY_ACCURATE in pulls and can_run_accurate() and model_for_quality(QUALITY_ACCURATE) in installed:
        default = model_for_quality(QUALITY_ACCURATE)
    elif model_for_quality(pulls[0]) in installed:
        default = model_for_quality(pulls[0])
    write_summary_model(DEFAULT_CONFIG_PATH, default)
    cfg = load_config()
    cfg.summary.model = default
    write_setting(cfg, "summary_model", default)
    emit(
        {
            "type": "log",
            "id": "ollama",
            "text": f"Yarn will use {quality_for_model(default).title()} ({default})",
        }
    )


def step_menubar(repo: Path) -> None:
    py = venv_python(repo)
    ma = repo / ".venv" / "bin" / "ma"
    if not existing_app("SwiftBar.app"):
        if not _brew_install("swiftbar", cask=True, step_id="menubar"):
            emit({"type": "log", "id": "menubar", "text": "Downloading SwiftBar for the menu bar icon"})
            app = install_app_from_zip(SWIFTBAR_ZIP, "SwiftBar.app", "menubar")
            subprocess.Popen(["open", "-a", str(app)])
    plugin_dir = Path.home() / ".swiftbar-plugins"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    for bundle in ("com.ameba.SwiftBar", "com.swiftbar.SwiftBar"):
        run(
            ["defaults", "write", bundle, "PluginDirectory", str(plugin_dir)],
            timeout=10,
        )
    env = {
        "PATH": f"{repo / '.venv' / 'bin'}:{os.environ.get('PATH', '')}",
        "YARN_SETUP_NONINTERACTIVE": "1",
    }
    installer = repo / "ui" / "install-ui.sh"
    emit({"type": "log", "id": "menubar", "text": "Installing menu bar and background jobs"})
    proc = run(["bash", str(installer)], env=env, timeout=600)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "install-ui.sh failed")[-2000:]
        raise RuntimeError(detail)
    emit({"type": "log", "id": "menubar", "text": "LaunchAgents loaded"})
    # Touch the microphone so macOS asks now, not mid-meeting.
    probe = run(
        [
            str(py),
            "-c",
            "import sounddevice as sd; sd.query_devices(); "
            "print('microphone list ok')",
        ],
        timeout=30,
    )
    if probe.returncode == 0:
        emit({"type": "log", "id": "menubar", "text": "Microphone list is visible to Yarn"})
    else:
        emit(
            {
                "type": "log",
                "id": "menubar",
                "text": "Allow Microphone for Yarn the first time you record.",
            }
        )
    emit({"type": "log", "id": "menubar", "text": f"Command is {ma}"})
    subprocess.Popen(["open", "-a", "SwiftBar"])


_PULL: list[str] = []


def requested_pulls() -> list[str]:
    from .resources import QUALITY_ACCURATE, QUALITY_FAST, can_run_accurate

    wanted = list(_PULL) if _PULL else (
        [QUALITY_FAST, QUALITY_ACCURATE] if can_run_accurate() else [QUALITY_FAST]
    )
    if not can_run_accurate():
        wanted = [q for q in wanted if q != QUALITY_ACCURATE] or [QUALITY_FAST]
    return wanted


HANDLERS = {
    "prereqs": step_prereqs,
    "venv": step_venv,
    "deps": step_deps,
    "tools": step_tools,
    "config": step_config,
    "whisper": step_whisper,
    "speakers": step_speakers,
    "ollama": step_ollama,
    "menubar": step_menubar,
}


def run_setup(repo: Path | None = None) -> int:
    repo = repo or repo_dir()
    emit({"type": "hello", "steps": [{"id": i, "title": t} for i, t in STEPS]})
    total = len(STEPS)
    for index, (step_id, title) in enumerate(STEPS, start=1):
        emit(
            {
                "type": "start",
                "id": step_id,
                "title": title,
                "index": index,
                "total": total,
            }
        )
        started = time.perf_counter()
        try:
            if step_id == "venv":
                repo = materialize_runtime(repo)
            HANDLERS[step_id](repo)
        except Exception as exc:
            emit(
                {
                    "type": "done",
                    "id": step_id,
                    "ok": False,
                    "elapsed_ms": int((time.perf_counter() - started) * 1000),
                    "message": str(exc),
                }
            )
            emit({"type": "finish", "ok": False, "message": str(exc)})
            return 1
        emit(
            {
                "type": "done",
                "id": step_id,
                "ok": True,
                "elapsed_ms": int((time.perf_counter() - started) * 1000),
            }
        )
    emit(
        {
            "type": "finish",
            "ok": True,
            "message": "Yarn is ready. Look for the ⏺ icon in the menu bar.",
        }
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    global _PULL
    args = list(sys.argv[1:] if argv is None else argv)
    repo = repo_dir()
    if "--repo" in args:
        i = args.index("--repo")
        repo = Path(args[i + 1]).expanduser().resolve()
        del args[i : i + 2]
    if "--pull" in args:
        i = args.index("--pull")
        _PULL = [part.strip().lower() for part in args[i + 1].split(",") if part.strip()]
        del args[i : i + 2]
    return run_setup(repo)


if __name__ == "__main__":
    raise SystemExit(main())
