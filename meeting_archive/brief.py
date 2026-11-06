"""Local daily brief - read-only HTML over recent meetings.

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
from .records import (
    Transcript,
    ensure_transcript_page,
    parse_meeting_id,
    write_text_atomic,
)
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
    # True when there is no written summary and the text is a transcript excerpt.
    summary_missing: bool = False
    language: str = ""


@dataclass
class BriefItem:
    text: str
    meeting_id: str = ""
    title: str = ""
    why: str = ""
    # True when the line was taken from a meeting summary, not drafted.
    grounded: bool = False


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
    generated_at: datetime | None = None


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
        written = (transcript.summary or "").strip()
        missing = not written
        if written:
            summary = written
        elif transcript.turns:
            # Keep the meeting visible. The excerpt is labelled missing so the
            # brief does not pretend a summary was written.
            summary = " ".join(t.text for t in transcript.turns[:12])
        else:
            continue
        try:
            page = ensure_transcript_page(cfg, transcript, notes)
        except (OSError, ValueError):
            page = cfg.transcript_md(transcript.meeting_id)
        found.append(
            MeetingBriefSource(
                meeting_id=transcript.meeting_id,
                title=transcript.title or notes.get("notes") or "Meeting",
                when=when,
                duration_s=float(transcript.duration_s or 0.0),
                attendees=str(notes.get("attendees") or ""),
                summary=summary[:SUMMARY_CHARS],
                md_uri=page.as_uri() if page.exists() else "",
                summary_missing=missing,
                language=transcript.language or "",
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
            f"ATTENDEES: {src.attendees or 'none'}\n"
            f"SUMMARY_STATUS: {'missing, excerpt only, not a written summary' if src.summary_missing else 'written'}\n"
            f"SUMMARY:\n{src.summary}\n"
        )
    return "\n---\n".join(blocks)


def _brief_prompt(context: str, day: date, *, other_languages: bool = False) -> str:
    heard = ""
    if other_languages:
        heard = (
            "The brief itself is in English. If a meeting was in another "
            "language, write the point in English and keep short quotes in "
            "the language that was spoken.\n"
        )
    opener = "You write a private morning brief from meeting notes only."
    if heard:
        opener = f"{opener}\n{heard.rstrip()}"
    return f"""{opener}
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
- If SUMMARY_STATUS says missing, that text is a transcript excerpt. Do not treat it as a finished summary.

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

    other = any((src.language or "en") not in {"", "en"} for src in sources)
    raw = summarizer._run(  # noqa: SLF001
        _brief_prompt(_context_blob(sources), today, other_languages=other)
    )
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
        generated_at=datetime.now(),
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


def _extracted_actions(sources: list[MeetingBriefSource]) -> list[BriefItem]:
    """Action lines already written in a meeting summary, not drafted again."""
    heading = re.compile(r"^\**\s*action items\**\s*:?\s*$", re.IGNORECASE)
    bullet = re.compile(r"^[-*•]\s+(.*)$")
    found: list[BriefItem] = []
    for src in sources:
        if src.summary_missing:
            continue
        capture = False
        for line in src.summary.splitlines():
            stripped = line.strip()
            if heading.match(stripped):
                capture = True
                continue
            if not capture:
                continue
            if not stripped:
                continue
            match = bullet.match(stripped)
            if not match:
                break
            text = match.group(1).strip()
            if text:
                found.append(
                    BriefItem(
                        text=text,
                        meeting_id=src.meeting_id,
                        title=src.title,
                        grounded=True,
                    )
                )
    return found[:8]


def _grounded(item: BriefItem, sources: list[MeetingBriefSource]) -> bool:
    if item.grounded:
        return True
    src = next(
        (
            s
            for s in sources
            if s.meeting_id == item.meeting_id and not s.summary_missing
        ),
        None,
    )
    if src is None:
        return False
    needle = " ".join(item.text.lower().split())
    if len(needle) < 12:
        return False
    hay = " ".join(src.summary.lower().split())
    return needle in hay


def _merge_items(drafted: list[BriefItem], extracted: list[BriefItem], sources) -> list[BriefItem]:
    out: list[BriefItem] = []
    seen: list[str] = []
    for item in [*extracted, *drafted]:
        key = " ".join(item.text.lower().split())
        if any(key and (key in prev or prev in key) for prev in seen):
            continue
        item.grounded = _grounded(item, sources)
        out.append(item)
        if key:
            seen.append(key)
    return out


def render_brief_html(brief: BriefResult, *, hint_login: bool = False) -> str:
    """Calm morning brief. Self-contained HTML, no network fonts or scripts."""
    from . import htmltheme

    day_label = brief.day.strftime("%A %d %B %Y").replace(" 0", " ")
    generated = brief.generated_at or datetime.now()
    generated_label = generated.strftime("%H:%M")
    hours = brief.hours_by_day
    max_h = max(hours.values(), default=0.0) or 1.0
    week_bars = []
    for key, value in hours.items():
        d = date.fromisoformat(key)
        height = max(8, int(72 * (value / max_h))) if value else 4
        label = d.strftime("%a")
        tip = f"{value:.1f}h" if value else "-"
        week_bars.append(
            f'<div class="bar-col"><div class="bar" style="height:{height}px" title="{_esc(tip)}"></div>'
            f"<span>{_esc(label)}</span><small>{_esc(tip)}</small></div>"
        )

    focus_rows = []
    for slice_ in brief.focus:
        focus_rows.append(
            f'<div class="focus-row"><span>{_esc(slice_.label)}</span>'
            f'<div class="focus-track"><div class="focus-fill" style="width:{slice_.weight:.0f}%"></div></div>'
            f"<em>{slice_.weight:.0f}%</em></div>"
        )

    actions = _merge_items(brief.actions, _extracted_actions(brief.sources), brief.sources)
    linked = [src for src in brief.sources if src.md_uri]
    week_hours = _compact_hours(sum(hours.values()))

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
            why = f'<div class="why">{_rich(item.why)}</div>' if item.why else ""
            kind = (
                '<span class="pill fact">From the meeting</span>'
                if _grounded(item, brief.sources)
                else '<span class="pill draft">Draft</span>'
            )
            lis.append(
                f'<li><div class="item">{_rich(item.text)}</div>{why}'
                f'<div class="row-meta">{kind}{source}</div></li>'
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
            "turn on <strong>Open brief at login</strong> in the Yarn menu. "
            "It stays off until you do.</p>"
        )

    sources = []
    missing = 0
    for src in brief.sources:
        if not src.md_uri:
            continue
        if src.summary_missing:
            missing += 1
        status = (
            '<span class="pill missing">Summary not written yet</span>'
            if src.summary_missing
            else ""
        )
        sources.append(
            f'<li><a href="{_esc(src.md_uri)}">{_esc(src.title)}</a> '
            f'<span>{_esc(src.when.strftime("%a %d %b %H:%M"))} · '
            f"{_esc(format_duration(src.duration_s))} {status}</span></li>"
        )
    gap = ""
    if missing:
        gap = (
            f'<p class="empty">{missing} meeting'
            f'{"s" if missing != 1 else ""} in this brief '
            f'{"have" if missing != 1 else "has"} no written summary yet. '
            "Those lines are drafts from a short excerpt.</p>"
        )

    overview = ""
    if brief.overview.strip():
        overview = (
            f'<section class="card prose overview"><h2>In short</h2>'
            f"{_blocks(brief.overview)}</section>"
        )
    body = f"""
<div class="dashboard">
  <div class="dash-top">
    <div>
      {htmltheme.masthead("Yarn daily brief", day_label)}
      <p class="when">Generated {generated_label} · saved on this Mac</p>
    </div>
    <div class="stats">
      <div class="stat"><strong>{len(linked)}</strong><span>Meetings</span></div>
      <div class="stat"><strong>{week_hours}</strong><span>Hours</span></div>
      <div class="stat"><strong>{len(actions)}</strong><span>Actions</span></div>
    </div>
  </div>
  {hint}
  {overview}
  <section class="card week">
    <h2>Where the week went</h2>
    <div class="bars">{"".join(week_bars)}</div>
  </section>
  <div class="columns boards">
    {section("Needs action", actions, "No open actions jumped out.")}
    {section("Important today", brief.important, "Nothing urgent stood out.")}
    {section("Start this week", brief.start, "No clear starts called out.")}
  </div>
  <section class="card focus-card">
    <h2>Where to spend today</h2>
    {"".join(focus_rows) if focus_rows else '<p class="empty">Not enough signal for a focus split.</p>'}
  </section>
  <section class="card sources meetings">
    <h2>From your meetings</h2>
    {gap}
    <ul>{"".join(sources) or "<li><span>No linked transcripts.</span></li>"}</ul>
  </section>
  <footer>
    Saved on this Mac. Drafted locally. Verify against the meetings.
    Model: {_esc(brief.model or "local")}.
    Read-only over your archive; nothing was changed.
  </footer>
</div>
"""
    return htmltheme.document(f"Yarn brief, {brief.day.isoformat()}", body)


def _compact_hours(total: float) -> str:
    if total <= 0:
        return "0"
    # A short meeting is still a meeting. Rounding it to 0 made the tile a lie.
    if total < 0.1:
        return "<0.1"
    rounded = round(total, 1)
    if float(rounded).is_integer():
        return str(int(rounded))
    return f"{rounded:.1f}"


def _rich(value: str) -> str:
    """One line of model text, with emphasis shown as bold."""
    from .markup import markup_inline

    return markup_inline(value)


def _blocks(value: str) -> str:
    from .markup import markup_to_html

    return markup_to_html(value)


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
