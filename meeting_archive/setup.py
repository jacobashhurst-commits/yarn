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
SUMMARY_MODEL = "qwen3.5:9b"
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


def venv_python(repo: Path) -> Path:
    return repo / ".venv" / "bin" / "python"


def support_bin() -> Path:
    path = Path.home() / "Library" / "Application Support" / "Yarn" / "bin"
    path.mkdir(parents=True, exist_ok=True)
    return path


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
    pip = run([str(py), "-m", "pip", "--version"], timeout=30)
    if pip.returncode == 0:
        proc = run([str(py), "-m", "pip", "install", "-e", str(repo)], timeout=600)
    elif which("uv"):
        proc = run(
            [which("uv"), "pip", "install", "--python", str(py), "-e", str(repo)],
            timeout=600,
        )
    else:
        run([str(py), "-m", "ensurepip", "--upgrade"], timeout=120)
        proc = run([str(py), "-m", "pip", "install", "-e", str(repo)], timeout=600)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "install failed").strip()[-2000:])
    ma = repo / ".venv" / "bin" / "ma"
    if not ma.is_file():
        raise RuntimeError("Yarn installed but the `ma` command is missing.")
    emit({"type": "log", "id": "deps", "text": f"ma is at {ma}"})


def _brew_install(formula: str, cask: bool = False) -> bool:
    brew = which("brew")
    if not brew:
        return False
    argv = [brew, "install"]
    if cask:
        argv.append("--cask")
    argv.append(formula)
    emit({"type": "log", "id": "tools", "text": " ".join(argv)})
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
    ffmpeg = which("ffmpeg") or str(support_bin() / "ffmpeg")
    proc = run(
        [ffmpeg, "-f", "avfoundation", "-list_devices", "true", "-i", ""],
        timeout=20,
    )
    text = (proc.stderr or "") + (proc.stdout or "")
    for line in text.splitlines():
        lower = line.lower()
        if "macbook" in lower and "micro" in lower:
            start = line.find("]")
            name = line[start + 1 :].strip() if start >= 0 else line.strip()
            return name.strip("[] ")
        if "built-in" in lower and "micro" in lower:
            start = line.find("]")
            name = line[start + 1 :].strip() if start >= 0 else line.strip()
            return name.strip("[] ")
    return ""


def step_config(repo: Path) -> None:
    from .config import DEFAULT_CONFIG_PATH, DEFAULT_ROOT

    DEFAULT_ROOT.mkdir(parents=True, exist_ok=True)
    (DEFAULT_ROOT / "models" / "whisper-cpp").mkdir(parents=True, exist_ok=True)
    (DEFAULT_ROOT / "models" / "diarization").mkdir(parents=True, exist_ok=True)
    DEFAULT_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DEFAULT_CONFIG_PATH.exists():
        emit(
            {
                "type": "log",
                "id": "config",
                "text": f"Keeping existing {DEFAULT_CONFIG_PATH}",
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
segmentation_model = "{root}/models/diarization/sherpa-onnx-pyannote-segmentation-3-0/model.onnx"
embedding_model = "{root}/models/diarization/speaker-embedding.onnx"
identify_popup = true
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


def step_speakers(repo: Path) -> None:
    from .config import load_config

    cfg = load_config()
    seg = Path(cfg.diarize.segmentation_model).expanduser()
    emb = Path(cfg.diarize.embedding_model).expanduser()
    if seg.is_file() and emb.is_file():
        emit({"type": "log", "id": "speakers", "text": "Speaker models already on disk"})
        return
    import tarfile

    tmp = Path.home() / "Library" / "Caches" / "Yarn"
    tmp.mkdir(parents=True, exist_ok=True)
    archive = tmp / "segmentation.tar.bz2"
    if not seg.is_file():
        download(DIARIZE_SEG, archive, "speakers")
        seg.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive, "r:bz2") as tar:
            kwargs = {"filter": "data"} if sys.version_info >= (3, 12) else {}
            tar.extractall(seg.parent, **kwargs)
        emit({"type": "log", "id": "speakers", "text": f"Segmentation model in {seg.parent}"})
    if not emb.is_file():
        download(DIARIZE_EMB, emb, "speakers")
    emit({"type": "log", "id": "speakers", "text": "Speaker models ready"})


def step_ollama(repo: Path) -> None:
    ollama = which("ollama")
    if not ollama:
        app = Path("/Applications/Ollama.app")
        if not app.exists():
            emit(
                {
                    "type": "log",
                    "id": "ollama",
                    "text": "Ollama is optional. Transcripts still save without it.",
                }
            )
            emit(
                {
                    "type": "log",
                    "id": "ollama",
                    "text": "Install it later from the Yarn menu: Open Ollama.",
                }
            )
            return
        subprocess.Popen(["open", "-a", "Ollama"])
        time.sleep(2)
        ollama = which("ollama")
    if not ollama:
        return
    emit({"type": "log", "id": "ollama", "text": f"Using {ollama}"})
    listed = run([ollama, "list"], timeout=60)
    if SUMMARY_MODEL in (listed.stdout or ""):
        emit({"type": "log", "id": "ollama", "text": f"{SUMMARY_MODEL} already installed"})
        return
    emit({"type": "log", "id": "ollama", "text": f"Pulling {SUMMARY_MODEL} (several GB)"})
    proc = run([ollama, "pull", SUMMARY_MODEL], timeout=3600)
    if proc.returncode != 0:
        emit(
            {
                "type": "log",
                "id": "ollama",
                "text": "Could not pull the summary model; summaries will wait.",
            }
        )
        return
    emit({"type": "log", "id": "ollama", "text": f"{SUMMARY_MODEL} ready"})


def step_menubar(repo: Path) -> None:
    py = venv_python(repo)
    ma = repo / ".venv" / "bin" / "ma"
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
    args = list(sys.argv[1:] if argv is None else argv)
    repo = repo_dir()
    if "--repo" in args:
        i = args.index("--repo")
        repo = Path(args[i + 1]).expanduser().resolve()
    return run_setup(repo)


if __name__ == "__main__":
    raise SystemExit(main())
