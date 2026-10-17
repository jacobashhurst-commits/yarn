"""Install Yarn on this Mac without asking anyone to touch Python.

`python3 -m meeting_archive.setup` is the worker the Setup window reads.
Each step prints one JSON line so the window can show 3 of 9, a tick, and
the current line of detail. Re-running is safe: finished work is skipped.
"""

from __future__ import annotations

import json
import os
import platform
import re
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
# macOS /usr/bin/python3 is still 3.9. It has no tomllib, and Setup used to
# stop on the first step with that raw import error. uv installs a current
# CPython into this user's Library, with no admin password.
UV_ARM64 = (
    "https://github.com/astral-sh/uv/releases/latest/download/"
    "uv-aarch64-apple-darwin.tar.gz"
)
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
    download(url, archive, step_id, long=step_id == "ollama")
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


LONG_DOWNLOAD_NOTE = (
    "Don't worry, this one takes a minute. "
    "Go get a coffee. When was the last time you called your mum?"
)


def download(url: str, dest: Path, step_id: str, *, long: bool = False) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_suffix(dest.suffix + ".partial")
    req = urllib.request.Request(url, headers={"User-Agent": "YarnSetup/0.2"})
    payload: dict = {"type": "log", "id": step_id, "text": f"Downloading {dest.name}"}
    if long:
        payload["note"] = LONG_DOWNLOAD_NOTE
    emit(payload)
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
                    percent = min(99, int(got * 100 / total))
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
    # The byte count can finish under the advertised length. The file is
    # on disk, so the bar has to be allowed to reach 100.
    emit({"type": "progress", "id": step_id, "percent": 100})


def python_supports_yarn(path: str) -> bool:
    if not path or not Path(path).is_file():
        return False
    proc = run(
        [
            path,
            "-c",
            "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)",
        ],
        timeout=20,
    )
    return proc.returncode == 0


def uv_binary() -> str | None:
    found = which("uv")
    if found:
        return found
    bundled = support_bin() / "uv"
    if bundled.is_file():
        return str(bundled)
    return None


def uv_python_find() -> str | None:
    uv = uv_binary()
    if not uv:
        return None
    proc = run([uv, "python", "find", "3.12"], timeout=30)
    lines = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
    path = lines[-1] if lines else ""
    if proc.returncode == 0 and python_supports_yarn(path):
        return path
    return None


def find_existing_python() -> str | None:
    # The interpreter already running Setup is the one the venv should copy.
    # Searching PATH first used to pick /usr/bin/python3, which is 3.9.
    if sys.version_info >= (3, 11) and Path(sys.executable).is_file():
        return sys.executable
    for name in ("python3.12", "python3.13", "python3.11", "python3"):
        path = which(name)
        if path and python_supports_yarn(path):
            return path
    return uv_python_find()


def _extract_uv(payload: bytes, dest: Path) -> None:
    import io
    import tarfile

    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
        member = next(
            (
                item
                for item in tar.getmembers()
                if Path(item.name).name == "uv" and item.isfile()
            ),
            None,
        )
        extracted = tar.extractfile(member) if member is not None else None
        if extracted is None:
            raise RuntimeError("The Python installer download did not contain uv.")
        data = extracted.read()
    dest.write_bytes(data)
    dest.chmod(0o755)


def _install_uv() -> None:
    if platform.machine() != "arm64":
        raise RuntimeError(
            "Yarn Setup is Apple Silicon only. Intel Macs are not supported."
        )
    archive = cache_dir() / "uv-aarch64-apple-darwin.tar.gz"
    download(UV_ARM64, archive, "prereqs", long=True)
    _extract_uv(archive.read_bytes(), support_bin() / "uv")


def install_host_python() -> str:
    """Install CPython 3.12 for this user when the Mac only has an older one."""
    uv = uv_binary()
    if not uv:
        _install_uv()
        uv = str(support_bin() / "uv")
    proc = run([uv, "python", "install", "3.12"], timeout=900)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "uv could not install Python").strip()
        raise RuntimeError(
            "Yarn needs Python 3.11 or newer. Installing Python 3.12 did not work.\n"
            + detail[-1500:]
        )
    found = uv_python_find()
    if not found:
        raise RuntimeError("Python 3.12 installed, but Yarn could not find it.")
    return found


def host_python() -> str:
    found = find_existing_python()
    if found:
        return found
    current = f"{sys.version_info[0]}.{sys.version_info[1]}"
    raise RuntimeError(
        f"This Mac has Python {current}. Yarn needs Python 3.11 or newer."
    )


def adopt_python311() -> None:
    """Replace this process with Python 3.11+ before anything imports tomllib."""
    if sys.version_info >= (3, 11):
        return
    current = f"{sys.version_info[0]}.{sys.version_info[1]}"
    emit(
        {
            "type": "log",
            "text": (
                f"This Mac has Python {current}. Yarn needs 3.11 or newer. "
                "Installing Python 3.12."
            ),
        }
    )
    found = find_existing_python()
    if not found:
        try:
            found = install_host_python()
        except RuntimeError as exc:
            emit({"type": "finish", "ok": False, "message": str(exc)})
            raise SystemExit(1) from exc
    if not found or os.path.realpath(found) == os.path.realpath(sys.executable):
        emit(
            {
                "type": "finish",
                "ok": False,
                "message": (
                    f"This Mac has Python {current}. Yarn needs Python 3.11 "
                    "or newer, and installing it did not work."
                ),
            }
        )
        raise SystemExit(1)
    os.execv(found, [found, *sys.argv])


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
    try:
        from .resources import summary_model_choice, unified_memory_gb
    except ModuleNotFoundError as exc:
        if exc.name == "tomllib":
            current = f"{sys.version_info[0]}.{sys.version_info[1]}"
            raise RuntimeError(
                f"This Mac has Python {current}. Yarn needs Python 3.11 or newer."
            ) from exc
        raise

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
    link_sherpa_runtime(site_packages(repo))
    verify_runtime_imports(repo)
    emit({"type": "log", "id": "deps", "text": f"ma is at {ma}"})
    emit({"type": "log", "id": "deps", "text": "Speaker identification can load"})


def site_packages(repo: Path) -> Path:
    py = venv_python(repo)
    proc = run(
        [str(py), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        timeout=30,
    )
    path = Path((proc.stdout or "").strip())
    if proc.returncode != 0 or not path.is_dir():
        raise RuntimeError("Could not find the Python environment.")
    return path


def link_sherpa_runtime(site: Path) -> None:
    """Put onnxruntime where the sherpa-onnx Mac wheel actually looks.

    The wheel links @rpath/libonnxruntime.dylib and does not ship that file.
    The installed onnxruntime library then asks for @rpath/libonnxruntime.1.dylib.
    Both names have to sit beside the sherpa extension, or every meeting is
    labelled with no speakers and the library says sherpa-onnx is not installed.
    """
    sherpa_lib = site / "sherpa_onnx" / "lib"
    dylibs = sorted((site / "onnxruntime" / "capi").glob("libonnxruntime*.dylib"))
    if not sherpa_lib.is_dir():
        raise RuntimeError("sherpa-onnx is missing from the install.")
    if not dylibs:
        raise RuntimeError(
            "onnxruntime is missing, so speaker identification cannot start."
        )
    real = dylibs[-1]
    for name in ("libonnxruntime.dylib", "libonnxruntime.1.dylib"):
        dest = sherpa_lib / name
        if dest.is_symlink() or dest.exists():
            if dest.is_symlink() and dest.resolve() == real.resolve():
                continue
            dest.unlink()
        dest.symlink_to(real)


def verify_runtime_imports(repo: Path) -> None:
    py = venv_python(repo)
    proc = run(
        [
            str(py),
            "-c",
            "import faster_whisper, sounddevice, chromadb, sherpa_onnx\n"
            "print('runtime imports ok')\n",
        ],
        timeout=120,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "import failed").strip()[-1500:]
        raise RuntimeError(f"Speaker identification could not start.\n{detail}")


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
            download(FFMPEG_STATIC, dest, "tools", long=True)
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


def faster_whisper_repo(model: str) -> str:
    """Hugging Face id faster-whisper loads for a short model name.

    Systran/faster-whisper-large-v3-turbo now returns 401 to an anonymous
    download. faster-whisper itself requests the mobius repo, which is public
    and redirects to the same files.
    """
    if "/" in model:
        return model
    if model in ("large-v3-turbo", "turbo"):
        return "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
    return f"Systran/faster-whisper-{model}"


def _plain_failure(detail: str) -> str:
    """The last real sentence of a tool failure, without a Python traceback."""
    lines = []
    for line in detail.splitlines():
        text = line.strip()
        if not text or text.startswith("File ") or text.startswith("Traceback"):
            continue
        if set(text) <= {"^"} or "site-packages" in text:
            continue
        lines.append(text)
    if not lines:
        return "the download did not finish"
    return lines[-1][:500]


def _prefetch_faster_whisper(repo: Path, model: str) -> None:
    """Put the speech weights on disk before the first meeting.

    Config sets local_files_only, so a meeting will not download them later.
    A Mac without Homebrew never gets whisper-cli, and this is the engine
    that install then selects. download_model uses the same repo id the
    transcriber will look up, so the cache actually hits.
    """
    repo_id = faster_whisper_repo(model)
    emit(
        {
            "type": "log",
            "id": "whisper",
            "text": f"Downloading speech model {repo_id}",
            "note": LONG_DOWNLOAD_NOTE,
        }
    )
    py = venv_python(repo)
    code = (
        "from faster_whisper.utils import download_model\n"
        f"print(download_model({repo_id!r}))\n"
    )
    proc = run([str(py), "-c", code], timeout=3600)
    if proc.returncode != 0:
        detail = _plain_failure(proc.stderr or proc.stdout or "")
        raise RuntimeError(f"Could not download the speech model. {detail}")
    emit({"type": "log", "id": "whisper", "text": "Speech model is on this Mac"})


def step_whisper(repo: Path) -> None:
    from .config import load_config

    cfg = load_config()
    if cfg.whisper.engine != "whisper_cpp":
        _prefetch_faster_whisper(repo, cfg.whisper.model)
        return
    path = Path(cfg.whisper.model_path).expanduser()
    if path.is_file() and path.stat().st_size > 100_000_000:
        emit({"type": "log", "id": "whisper", "text": f"Already have {path.name}"})
        return
    download(WHISPER_GGML, path, "whisper", long=True)
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
        download(DIARIZE_SEG, archive, "speakers", long=True)
        extract_segmentation_archive(archive, seg)
        emit({"type": "log", "id": "speakers", "text": f"Segmentation model in {seg.parent}"})
    if not emb.is_file():
        download(DIARIZE_EMB, emb, "speakers", long=True)
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


def ollama_model_names(list_output: str) -> set[str]:
    """Names from `ollama list`, skipping the header the same way summaries do."""
    names: set[str] = set()
    for line in list_output.splitlines()[1:]:
        parts = line.split()
        if parts:
            names.add(parts[0])
    return names


def _percent_in(line: str) -> int | None:
    match = re.search(r"(\d{1,3})\s*%", line)
    if not match:
        return None
    return min(100, int(match.group(1)))


# Ollama redraws one status line with cursor codes. Shown raw, a dropped
# download looks like "[?2026h[?25l[1Gpulling manifest".
_CSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_CONTROL = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b.")

# A dropped connection. `ollama pull` keeps the layers it already wrote.
_TRANSIENT_DOWNLOAD = (
    "unexpected eof",
    "connection reset",
    "broken pipe",
    "i/o timeout",
    "timeout",
    "temporarily unavailable",
    "tls handshake",
    "connection refused",
    "network is unreachable",
    "stream error",
)
PULL_ATTEMPTS = 4


def _take_status(pending: str) -> tuple[list[str], str]:
    """Split a chunk of Ollama's redrawn status. Keep a cut-off escape."""
    hold = ""
    cut = pending.rfind("\x1b")
    if cut != -1:
        tail = pending[cut:]
        unfinished = tail == "\x1b" or (
            tail.startswith("\x1b[") and _CSI.match(tail) is None
        )
        if unfinished:
            hold = tail
            pending = pending[:cut]
    cleaned = _CONTROL.sub("\n", pending.replace("\r", "\n"))
    parts = [part.strip() for part in cleaned.split("\n")]
    rest = parts.pop() if parts else ""
    # A following escape means the text before it was one finished redraw.
    if hold:
        if rest:
            parts.append(rest)
        rest = hold
    return [part for part in parts if part], rest


def _transient_download(detail: str) -> bool:
    text = detail.lower()
    return any(needle in text for needle in _TRANSIENT_DOWNLOAD)


def ensure_ollama_ready(binary: str) -> str:
    """Return `ollama list` output once the app is actually answering."""
    probe = run([binary, "list"], timeout=20)
    if probe.returncode == 0:
        return probe.stdout or ""
    subprocess.Popen(["open", "-a", "Ollama"])
    last = (probe.stderr or probe.stdout or "").strip()
    for _ in range(30):
        time.sleep(2)
        probe = run([binary, "list"], timeout=20)
        if probe.returncode == 0:
            return probe.stdout or ""
        last = (probe.stderr or probe.stdout or last).strip()
    raise RuntimeError(
        "Ollama did not start"
        + (f" ({last[-300:]})" if last else "")
        + ". Open Ollama, then run Yarn Setup again."
    )


def pull_ollama_model(binary: str, model: str, label: str) -> None:
    emit(
        {
            "type": "log",
            "id": "ollama",
            "text": f"Downloading {label} ({model})",
            "note": LONG_DOWNLOAD_NOTE,
        }
    )
    last_error = ""
    for attempt in range(1, PULL_ATTEMPTS + 1):
        if attempt > 1:
            # The first connection often closes before any bytes land. The
            # next pull continues, so the window should keep saying that it
            # is downloading. "Dropped" sat on screen for the whole retry
            # and looked like the install had stopped.
            emit(
                {
                    "type": "log",
                    "id": "ollama",
                    "text": f"Downloading {label} ({model})",
                    "note": "The connection blinked. Continuing from what already arrived.",
                }
            )
            time.sleep(min(8, 2 * attempt))
        try:
            _pull_ollama_once(binary, model, label)
            return
        except RuntimeError as exc:
            last_error = str(exc)
            if attempt == PULL_ATTEMPTS or not _transient_download(last_error):
                raise
    raise RuntimeError(last_error)


def _pull_ollama_once(binary: str, model: str, label: str) -> None:
    proc = subprocess.Popen(
        [binary, "pull", model],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    last = -1
    tail: list[str] = []
    stream = proc.stdout
    pending = ""
    if stream is not None:
        while True:
            chunk = stream.read(256)
            if not chunk:
                break
            lines, pending = _take_status(pending + chunk)
            for clean in lines:
                tail.append(clean)
                tail[:] = tail[-8:]
                percent = _percent_in(clean)
                if percent is not None and percent != last:
                    last = percent
                    emit({"type": "progress", "id": "ollama", "percent": min(percent, 99)})
    if pending.strip():
        tail.append(pending.strip())
    code = proc.wait(timeout=3600)
    if code != 0:
        detail = " ".join(tail)[-400:]
        raise RuntimeError(
            f"Could not download {label} ({model})"
            + (f": {detail}" if detail else "")
        )
    emit({"type": "progress", "id": "ollama", "percent": 100})


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
            raise RuntimeError(
                f"Could not install Ollama ({exc}). Open Ollama, then run Yarn Setup again."
            ) from exc
        subprocess.Popen(["open", "-a", str(app)])
        for _ in range(20):
            time.sleep(2)
            ollama = ollama_binary()
            if ollama:
                break
    if not ollama:
        raise RuntimeError(
            "Ollama is installed but not ready yet. Open it once, then run Yarn Setup again."
        )
    from .config import DEFAULT_CONFIG_PATH, load_config, write_setting
    from .resources import (
        QUALITY_ACCURATE,
        can_run_accurate,
        model_for_quality,
        quality_for_model,
        summary_model_choice,
    )

    pulls = requested_pulls()
    emit({"type": "log", "id": "ollama", "text": f"Using {ollama}"})
    installed = ollama_model_names(ensure_ollama_ready(ollama))
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
        pull_ollama_model(ollama, model, label)
        installed = ollama_model_names(ensure_ollama_ready(ollama))
        if model not in installed:
            raise RuntimeError(
                f"{label} ({model}) did not appear in Ollama after the download."
            )
        emit({"type": "log", "id": "ollama", "text": f"{label} ready"})
    default, _ = summary_model_choice()
    accurate = model_for_quality(QUALITY_ACCURATE)
    if QUALITY_ACCURATE in pulls and can_run_accurate() and accurate in installed:
        default = accurate
    elif model_for_quality(pulls[0]) in installed:
        default = model_for_quality(pulls[0])
    if default not in installed:
        raise RuntimeError(
            f"Ollama model {default} is not installed. "
            "Yarn will not pretend a summary model is ready."
        )
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
    _enable_local_search(repo)


def _enable_local_search(repo: Path) -> None:
    """Leave Ask Yarn ready. Claude and ChatGPT stay optional."""
    from .config import load_config
    from .connect import install_search_server

    try:
        install_search_server(load_config())
    except OSError as exc:
        emit(
            {
                "type": "log",
                "id": "ollama",
                "text": f"Search can wait ({exc}). Ask Yarn will try again from the menu.",
            }
        )
        return
    _prefetch_search_embedder(repo)
    emit(
        {
            "type": "log",
            "id": "ollama",
            "text": "Ask Yarn is ready. It uses Ollama on this Mac.",
        }
    )


def _prefetch_search_embedder(repo: Path) -> None:
    """Cache Chroma's local embedder so the first question is not a download."""
    py = venv_python(repo)
    code = (
        "from chromadb.utils.embedding_functions import DefaultEmbeddingFunction\n"
        "DefaultEmbeddingFunction()(['ready'])\n"
    )
    proc = run([str(py), "-c", code], timeout=600)
    if proc.returncode != 0:
        emit(
            {
                "type": "log",
                "id": "ollama",
                "text": "Search can wait until the first meeting is indexed.",
            }
        )
        return
    emit({"type": "log", "id": "ollama", "text": "Search is ready on this Mac"})


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
    dock = Path.home() / "Applications" / "Yarn.app"
    if (dock / "Contents" / "MacOS" / "Yarn").is_file():
        emit({"type": "log", "id": "menubar", "text": f"Yarn is in the Dock ({dock})"})
    else:
        emit(
            {
                "type": "log",
                "id": "menubar",
                "text": (
                    "The Dock app was not built. The menu bar still works. "
                    "Install Xcode Command Line Tools, then run Yarn Setup again."
                ),
            }
        )
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
            "message": (
                "Yarn is in the Dock. Click it to record, watch progress, "
                "name voices, or ask about your meetings. The menu bar has "
                "the same controls. Everything stays on this Mac."
            ),
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
    os.environ["PATH"] = f"{support_bin()}:{os.environ.get('PATH', '')}"
    adopt_python311()
    return run_setup(repo)


if __name__ == "__main__":
    raise SystemExit(main())
