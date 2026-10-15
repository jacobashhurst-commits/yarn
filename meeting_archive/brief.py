"""Local daily brief — read-only HTML over recent meetings.

Does not touch capture, inbox, jobs, or diarization. It only reads transcripts
and notes, asks the already-running Ollama model for a short draft, and writes
`briefs/YYYY-MM-DD.html` plus `briefs/latest.html`.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from . import records
from .config import Config, write_setting
from .models import format_duration
from .records import Transcript, parse_meeting_id, write_text_atomic
from .summarize import Summarizer, SummaryUnavailable

log = logging.getLogger(__name__)

LOOKBACK_DAYS = 14
MAX_MEETINGS = 18
SUMMARY_CHARS = 1800
MANUAL_USES_BEFORE_HINT = 3

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


@dataclass
class MeetingBriefSource:
    meeting_id: str
    title: str
    when: datetime
    duration_s: float
    attendees: str
    summary: str
    md_uri: str


@dataclass
class BriefItem:
    text: str
    meeting_id: str = ""
    title: str = ""
    why: str = ""


@dataclass
class FocusSlice:
    label: str
    weight: float


@dataclass
class BriefResult:
    day: date
    overview: str
    actions: list[BriefItem] = field(default_factory=list)
    important: list[BriefItem] = field(default_factory=list)
    start: list[BriefItem] = field(default_factory=list)
    focus: list[FocusSlice] = field(default_factory=list)
    sources: list[MeetingBriefSource] = field(default_factory=list)
    hours_by_day: dict[str, float] = field(default_factory=dict)
    model: str = ""
    path: Path | None = None


def briefs_dir(cfg: Config) -> Path:
    return cfg.root / "briefs"


def brief_path(cfg: Config, day: date | None = None) -> Path:
    day = day or date.today()
    return briefs_dir(cfg) / f"{day.isoformat()}.html"


def latest_brief_path(cfg: Config) -> Path:
    return briefs_dir(cfg) / "latest.html"


def settings_overlay(cfg: Config) -> dict:
    path = cfg.root / ".yarn-settings.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def brief_on_login_enabled(cfg: Config) -> bool:
    return bool(settings_overlay(cfg).get("brief_on_login"))


def manual_brief_count(cfg: Config) -> int:
    try:
        return int(settings_overlay(cfg).get("brief_manual_count") or 0)
    except (TypeError, ValueError):
        return 0


def should_hint_login_brief(cfg: Config) -> bool:
    data = settings_overlay(cfg)
    if "brief_on_login" in data:
        return False
    return manual_brief_count(cfg) >= MANUAL_USES_BEFORE_HINT


def gather_sources(cfg: Config, *, today: date | None = None) -> list[MeetingBriefSource]:
    """Recent meetings with enough text to brief from. Newest first."""
    today = today or date.today()
    earliest = today - timedelta(days=LOOKBACK_DAYS)
    found: list[MeetingBriefSource] = []
    if not cfg.transcripts_dir.exists():
        return found
    for path in sorted(cfg.transcripts_dir.rglob("*.json"), reverse=True):
        try:
            transcript = Transcript.read(path)
            when = parse_meeting_id(transcript.meeting_id)
        except (OSError, ValueError):
            continue
        if when.date() < earliest:
            continue
        notes = records.read_notes(cfg.notes_json(transcript.meeting_id))
        summary = (transcript.summary or "").strip()
        if not summary and transcript.turns:
            # Fall back to a short raw excerpt so brief still works before
            # per-meeting summaries are filled.
            summary = " ".join(t.text for t in transcript.turns[:12])[:SUMMARY_CHARS]
        if not summary.strip():
            continue
        md = cfg.transcript_md(transcript.meeting_id)
        found.append(
            MeetingBriefSource(
                meeting_id=transcript.meeting_id,
                title=transcript.title or notes.get("notes") or "Meeting",
                when=when,
                duration_s=float(transcript.duration_s or 0.0),
                attendees=str(notes.get("attendees") or ""),
                summary=summary[:SUMMARY_CHARS],
                md_uri=md.as_uri() if md.exists() else "",
            )
        )
        if len(found) >= MAX_MEETINGS:
            break
    return found


def hours_by_day(sources: list[MeetingBriefSource], *, today: date | None = None) -> dict[str, float]:
    """Meeting hours for the last 7 calendar days (including empty days)."""
    today = today or date.today()
    days = [(today - timedelta(days=offset)) for offset in range(6, -1, -1)]
    totals = {d.isoformat(): 0.0 for d in days}
    for src in sources:
        key = src.when.date().isoformat()
        if key in totals:
            totals[key] += src.duration_s / 3600.0
    return totals


def _context_blob(sources: list[MeetingBriefSource]) -> str:
    blocks: list[str] = []
    for src in sources:
        blocks.append(
            f"MEETING_ID: {src.meeting_id}\n"
            f"TITLE: {src.title}\n"
            f"WHEN: {src.when.isoformat(timespec='minutes')}\n"
            f"DURATION: {format_duration(src.duration_s)}\n"
            f"ATTENDEES: {src.attendees or '—'}\n"
            f"SUMMARY:\n{src.summary}\n"
        )
    return "\n---\n".join(blocks)


def _brief_prompt(context: str, day: date) -> str:
    return f"""You write a private morning brief from meeting notes only.
Today is {day.isoformat()}. Look at roughly the last two weeks, and what
still matters for today and the coming week.

Use ONLY facts present below. Do not invent calendar events, deadlines, or
owners. If something is unclear, omit it. Prefer concrete next steps.

Return ONLY compact JSON (no markdown fences) with this shape:
{{
  "overview": "two sentences",
  "actions": [{{"text": "...", "meeting_id": "...", "why": "..."}}],
  "important": [{{"text": "...", "meeting_id": "...", "why": "..."}}],
  "start": [{{"text": "...", "meeting_id": "...", "why": "..."}}],
  "focus": [{{"label": "theme or account", "weight": 35}}]
}}

Rules:
- actions = commitments / follow-ups that still look open
- important = decisions, risks, or blockers to keep in mind today
- start = work that should begin this week (scoping, decks, outreach)
- focus weights are relative attention for today (numbers ~ sum to 100)
- meeting_id must be copied exactly from MEETING_ID when you cite a source
- 3-7 items per list max; empty lists are allowed

MEETINGS
{context}
"""


def _parse_model_json(text: str) -> dict:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = _JSON_BLOCK.search(text)
        if not match:
            raise
        return json.loads(match.group(0))


def _items(raw, sources: list[MeetingBriefSource]) -> list[BriefItem]:
    by_id = {s.meeting_id: s for s in sources}
    out: list[BriefItem] = []
    if not isinstance(raw, list):
        return out
    for row in raw[:8]:
        if not isinstance(row, dict):
            continue
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        mid = str(row.get("meeting_id") or "").strip()
        src = by_id.get(mid)
        out.append(
            BriefItem(
                text=text,
                meeting_id=mid if src else "",
                title=(src.title if src else ""),
                why=str(row.get("why") or "").strip(),
            )
        )
    return out


def _focus(raw) -> list[FocusSlice]:
    out: list[FocusSlice] = []
    if not isinstance(raw, list):
        return out
    for row in raw[:8]:
        if not isinstance(row, dict):
            continue
        label = str(row.get("label") or "").strip()
        if not label:
            continue
        try:
            weight = float(row.get("weight") or 0.0)
        except (TypeError, ValueError):
            weight = 0.0
        if weight <= 0:
            continue
        out.append(FocusSlice(label=label[:40], weight=weight))
    total = sum(f.weight for f in out) or 1.0
    for slice_ in out:
        slice_.weight = 100.0 * slice_.weight / total
    return out


def generate_brief(
    cfg: Config,
    summarizer: Summarizer | None = None,
    *,
    today: date | None = None,
    open_after: bool = False,
    login: bool = False,
    force: bool = False,
) -> BriefResult:
    """Build today's brief HTML. Raises SummaryUnavailable if Ollama is off."""
    today = today or date.today()
    if login and not brief_on_login_enabled(cfg) and not force:
        raise SummaryUnavailable("daily brief on login is off")

    path = brief_path(cfg, today)
    if login and path.exists() and not force:
        result = BriefResult(day=today, overview="already written", path=path)
        return result

    sources = gather_sources(cfg, today=today)
    if not sources:
        raise SummaryUnavailable("no recent meetings with text to brief from")

    summarizer = summarizer or Summarizer(cfg.summary)
    if not summarizer.enabled:
        raise SummaryUnavailable("summaries are disabled in config")
    if not summarizer.is_running():
        raise SummaryUnavailable(
            "Ollama is not running. Open Ollama, then run Daily brief again."
        )

    raw = summarizer._run(_brief_prompt(_context_blob(sources), today))  # noqa: SLF001
    try:
        payload = _parse_model_json(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise SummaryUnavailable(
            "local model returned a brief that was not usable JSON"
        ) from exc

    result = BriefResult(
        day=today,
        overview=str(payload.get("overview") or "").strip()
        or "Here is what stands out from your recent meetings.",
        actions=_items(payload.get("actions"), sources),
        important=_items(payload.get("important"), sources),
        start=_items(payload.get("start"), sources),
        focus=_focus(payload.get("focus")),
        sources=sources,
        hours_by_day=hours_by_day(sources, today=today),
        model=cfg.summary.model,
    )
    if not result.focus:
        result.focus = _focus_from_titles(sources)

    html = render_brief_html(result, hint_login=should_hint_login_brief(cfg) and not login)
    briefs_dir(cfg).mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, html)
    write_text_atomic(latest_brief_path(cfg), html)
    result.path = path

    if not login:
        count = manual_brief_count(cfg) + 1
        write_setting(cfg, "brief_manual_count", count)

    if open_after:
        _open_path(path)
    return result


def _focus_from_titles(sources: list[MeetingBriefSource]) -> list[FocusSlice]:
    """Fallback attention chart from meeting titles when the model omits focus."""
    weights: dict[str, float] = defaultdict(float)
    for src in sources:
        label = _theme_label(src.title)
        weights[label] += max(src.duration_s, 60.0)
    ranked = sorted(weights.items(), key=lambda kv: kv[1], reverse=True)[:6]
    total = sum(w for _, w in ranked) or 1.0
    return [FocusSlice(label=label, weight=100.0 * w / total) for label, w in ranked]


def _theme_label(title: str) -> str:
    words = re.findall(r"[A-Za-z][A-Za-z0-9/+-]{1,20}", title or "")
    stop = {
        "meeting", "call", "with", "the", "and", "for", "demo", "weekly",
        "catchup", "internal", "discussion",
    }
    kept = [w for w in words if w.lower() not in stop][:2]
    return " ".join(kept) if kept else (title[:24] or "Other")


def render_brief_html(brief: BriefResult, *, hint_login: bool = False) -> str:
    """Aesthetic, self-contained HTML. No network fonts or scripts required."""
    day_label = brief.day.strftime("%A %d %B %Y").replace(" 0", " ")
    hours = brief.hours_by_day
    max_h = max(hours.values(), default=0.0) or 1.0
    week_bars = []
    for key, value in hours.items():
        d = date.fromisoformat(key)
        height = max(8, int(140 * (value / max_h))) if value else 4
        label = d.strftime("%a")
        tip = f"{value:.1f}h" if value else "—"
        week_bars.append(
            f'<div class="bar-col"><div class="bar" style="height:{height}px" title="{_esc(tip)}"></div>'
            f'<span>{_esc(label)}</span><small>{_esc(tip)}</small></div>'
        )

    focus_rows = []
    for slice_ in brief.focus:
        focus_rows.append(
            f'<div class="focus-row"><span>{_esc(slice_.label)}</span>'
            f'<div class="focus-track"><div class="focus-fill" style="width:{slice_.weight:.0f}%"></div></div>'
            f'<em>{slice_.weight:.0f}%</em></div>'
        )

    def section(title: str, items: list[BriefItem], empty: str) -> str:
        if not items:
            return (
                f'<section class="card"><h2>{_esc(title)}</h2>'
                f'<p class="empty">{_esc(empty)}</p></section>'
            )
        lis = []
        for item in items:
            source = ""
            if item.meeting_id and item.title:
                href = next(
                    (s.md_uri for s in brief.sources if s.meeting_id == item.meeting_id),
                    "",
                )
                label = _esc(item.title)
                source = (
                    f'<a class="src" href="{_esc(href)}">{label}</a>'
                    if href
                    else f'<span class="src">{label}</span>'
                )
            why = f'<div class="why">{_esc(item.why)}</div>' if item.why else ""
            lis.append(
                f"<li><div class=\"item\">{_esc(item.text)}</div>{why}{source}</li>"
            )
        return (
            f'<section class="card"><h2>{_esc(title)}</h2><ol class="items">'
            + "".join(lis)
            + "</ol></section>"
        )

    hint = ""
    if hint_login:
        hint = (
            '<p class="hint">You’ve built a few briefs by hand. When it feels useful, '
            "turn on <strong>Open brief at login</strong> in the Yarn menu — "
            "it stays off until you do.</p>"
        )

    sources = "".join(
        f'<li><a href="{_esc(s.md_uri)}">{_esc(s.title)}</a> '
        f'<span>{_esc(s.when.strftime("%a %d %b %H:%M"))} · '
        f"{_esc(format_duration(s.duration_s))}</span></li>"
        for s in brief.sources
        if s.md_uri
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Yarn brief — {brief.day.isoformat()}</title>
<style>
:root {{
  --ink: #15202b;
  --muted: #5c6b7a;
  --line: #c9d4df;
  --paper: #f3f6f9;
  --card: #ffffff;
  --sea: #1f6f6a;
  --sea-deep: #14524e;
  --sand: #e7eef4;
  --warn: #8a5a16;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  color: var(--ink);
  background:
    radial-gradient(1200px 500px at 10% -10%, #d7e6ef 0%, transparent 55%),
    radial-gradient(900px 420px at 100% 0%, #d9ebe7 0%, transparent 50%),
    var(--paper);
  font: 16px/1.45 "Avenir Next", "Segoe UI", sans-serif;
}}
.wrap {{
  max-width: 980px;
  margin: 0 auto;
  padding: 40px 28px 72px;
}}
header.hero {{
  display: grid;
  gap: 10px;
  margin-bottom: 28px;
}}
.eyebrow {{
  letter-spacing: 0.14em;
  text-transform: uppercase;
  font-size: 12px;
  color: var(--sea-deep);
  font-weight: 700;
}}
h1 {{
  margin: 0;
  font-family: "Avenir Next", "Segoe UI", sans-serif;
  font-size: clamp(2rem, 4vw, 2.8rem);
  line-height: 1.1;
  font-weight: 700;
  letter-spacing: -0.02em;
}}
.overview {{
  max-width: 46rem;
  color: var(--muted);
  font-size: 1.05rem;
}}
.hint {{
  margin: 0;
  padding: 12px 14px;
  border-left: 3px solid var(--sea);
  background: rgba(31, 111, 106, 0.08);
  color: var(--sea-deep);
}}
.grid {{
  display: grid;
  grid-template-columns: 1.1fr 0.9fr;
  gap: 18px;
  margin: 22px 0 18px;
}}
@media (max-width: 820px) {{
  .grid {{ grid-template-columns: 1fr; }}
}}
.card {{
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 18px;
  padding: 18px 18px 16px;
  box-shadow: 0 10px 30px rgba(21, 32, 43, 0.04);
}}
.card h2 {{
  margin: 0 0 14px;
  font-size: 0.95rem;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--sea-deep);
}}
.bars {{
  display: flex;
  align-items: flex-end;
  gap: 10px;
  min-height: 180px;
  padding-top: 8px;
}}
.bar-col {{
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  color: var(--muted);
  font-size: 12px;
}}
.bar {{
  width: 100%;
  max-width: 42px;
  border-radius: 10px 10px 4px 4px;
  background: linear-gradient(180deg, #3aa39a, var(--sea-deep));
}}
.bar-col small {{ color: var(--ink); font-weight: 600; }}
.focus-row {{
  display: grid;
  grid-template-columns: 7.5rem 1fr 2.5rem;
  gap: 10px;
  align-items: center;
  margin: 0 0 10px;
  font-size: 14px;
}}
.focus-track {{
  height: 12px;
  background: var(--sand);
  border-radius: 999px;
  overflow: hidden;
}}
.focus-fill {{
  height: 100%;
  background: linear-gradient(90deg, var(--sea), #3aa39a);
  border-radius: 999px;
}}
.focus-row em {{
  font-style: normal;
  text-align: right;
  color: var(--muted);
  font-variant-numeric: tabular-nums;
}}
.columns {{
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 18px;
}}
@media (max-width: 900px) {{
  .columns {{ grid-template-columns: 1fr; }}
}}
ol.items {{
  margin: 0;
  padding: 0;
  list-style: none;
  counter-reset: item;
}}
ol.items li {{
  counter-increment: item;
  padding: 12px 0 12px 2rem;
  border-top: 1px solid var(--line);
  position: relative;
}}
ol.items li:first-child {{ border-top: 0; padding-top: 0; }}
ol.items li::before {{
  content: counter(item);
  position: absolute;
  left: 0;
  top: 12px;
  width: 1.4rem;
  height: 1.4rem;
  border-radius: 999px;
  background: var(--sand);
  color: var(--sea-deep);
  font-size: 12px;
  font-weight: 700;
  display: grid;
  place-items: center;
}}
ol.items li:first-child::before {{ top: 0; }}
.item {{ font-weight: 600; }}
.why {{ color: var(--muted); font-size: 0.92rem; margin-top: 4px; }}
.src {{
  display: inline-block;
  margin-top: 6px;
  font-size: 0.82rem;
  color: var(--sea-deep);
  text-decoration: none;
  border-bottom: 1px solid rgba(20, 82, 78, 0.35);
}}
.empty {{ color: var(--muted); margin: 0; }}
.sources {{
  margin-top: 22px;
}}
.sources ul {{
  margin: 0;
  padding: 0;
  list-style: none;
  display: grid;
  gap: 8px;
}}
.sources li {{
  display: flex;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  padding: 8px 0;
  border-bottom: 1px solid var(--line);
}}
.sources a {{ color: var(--ink); font-weight: 600; text-decoration: none; }}
.sources span {{ color: var(--muted); font-size: 0.9rem; }}
footer {{
  margin-top: 28px;
  color: var(--muted);
  font-size: 0.85rem;
}}
</style>
</head>
<body>
<main class="wrap">
  <header class="hero">
    <div class="eyebrow">Yarn daily brief</div>
    <h1>{_esc(day_label)}</h1>
    <p class="overview">{_esc(brief.overview)}</p>
    {hint}
  </header>

  <div class="grid">
    <section class="card">
      <h2>Where the week went</h2>
      <div class="bars">{"".join(week_bars)}</div>
    </section>
    <section class="card">
      <h2>Where to spend today</h2>
      {"".join(focus_rows) if focus_rows else '<p class="empty">Not enough signal for a focus split.</p>'}
    </section>
  </div>

  <div class="columns">
    {section("Needs action", brief.actions, "No open actions jumped out.")}
    {section("Important today", brief.important, "Nothing urgent stood out.")}
    {section("Start this week", brief.start, "No clear starts called out.")}
  </div>

  <section class="card sources">
    <h2>From your meetings</h2>
    <ul>{sources or "<li><span>No linked transcripts.</span></li>"}</ul>
  </section>

  <footer>
    Draft only — verify against the meetings.
    Model: {_esc(brief.model or "local")}.
    Read-only over your archive; nothing was changed.
  </footer>
</main>
</body>
</html>
"""


def _esc(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _open_path(path: Path) -> None:
    import subprocess

    subprocess.Popen(["/usr/bin/open", str(path)])  # noqa: S603


def install_login_agent(cfg: Config, ma_bin: str) -> Path:
    """Write a LaunchAgent that runs `ma brief --login` after login."""
    label = "com.meetingarchive.brief"
    plist = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    plist.parent.mkdir(parents=True, exist_ok=True)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{ma_bin}</string>
    <string>brief</string>
    <string>--login</string>
    <string>--open</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>{cfg.logs_dir / "brief.out.log"}</string>
  <key>StandardErrorPath</key><string>{cfg.logs_dir / "brief.err.log"}</string>
  <key>WorkingDirectory</key><string>{cfg.root}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
</dict>
</plist>
"""
    plist.write_text(content, encoding="utf-8")
    _launchctl(["bootout", f"gui/{_uid()}/{label}"])
    _launchctl(["bootstrap", f"gui/{_uid()}", str(plist)])
    return plist


def uninstall_login_agent() -> None:
    label = "com.meetingarchive.brief"
    plist = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    _launchctl(["bootout", f"gui/{_uid()}/{label}"])
    plist.unlink(missing_ok=True)


def _uid() -> int:
    import os

    return os.getuid()


def _launchctl(args: list[str]) -> None:
    import subprocess

    subprocess.run(["launchctl", *args], capture_output=True, text=True)  # noqa: S603
