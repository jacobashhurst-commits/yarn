"""Shared local HTML for the brief, library, and speaker pages.

Tokens follow the Yarn brand guide (15 Oct 2026): SF Pro for reading,
JetBrains Mono for the wordmark and metadata, Yarn Navy / Blue / Ink.
No network fonts. The wool mark is the approved symbol, cropped from the
guide so these pages stay self-contained on disk.
"""

from __future__ import annotations

import base64
from functools import lru_cache
from pathlib import Path

_MARK = Path(__file__).with_name("assets") / "yarn-mark.png"

CSS = """
:root {
  --navy: #0D244B;
  --blue: #167DFF;
  --cyan: #48C8FA;
  --ink: #19293D;
  --slate: #617188;
  --cloud: #EDF6FF;
  --canvas: #FFFFFF;
  --card: #FFFFFF;
  --line: #d7e3ef;
  --warn: #8a5a16;
  --err: #9b2c2c;
  --radius: 12px;
  color-scheme: light dark;
}
@media (prefers-color-scheme: dark) {
  :root {
    --navy: #d6e4f5;
    --ink: #e7eef6;
    --slate: #9aafc3;
    --cloud: #172433;
    --canvas: #101820;
    --card: #172433;
    --line: #2a3c4e;
    --warn: #e0b15a;
    --err: #ffb4b4;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0;
  color: var(--ink);
  background: var(--canvas);
  font: 16px/1.5 -apple-system, BlinkMacSystemFont, "SF Pro Text", "SF Pro", sans-serif;
}
.wrap { max-width: 920px; margin: 0 auto; padding: 32px 24px 64px; }
.mast { display: flex; gap: 16px; align-items: center; margin: 0 0 8px; }
.mark { width: 96px; height: auto; display: block; flex: none; }
.wordmark {
  margin: 0;
  font-family: "JetBrains Mono", "SF Mono", ui-monospace, Menlo, monospace;
  font-weight: 500;
  font-size: 20px;
  letter-spacing: -0.03em;
  color: var(--navy);
}
.eyebrow {
  margin: 4px 0 0;
  font-family: "JetBrains Mono", "SF Mono", ui-monospace, Menlo, monospace;
  font-size: 12px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--navy);
}
h1 {
  margin: 8px 0 0;
  font-size: 28px;
  line-height: 1.2;
  font-weight: 600;
  letter-spacing: -0.02em;
  color: var(--navy);
}
.when, .mono {
  font-family: "JetBrains Mono", "SF Mono", ui-monospace, Menlo, monospace;
  font-size: 12px;
  color: var(--slate);
}
.when { margin: 8px 0 16px; }
.overview { max-width: 42rem; margin: 0 0 16px; font-size: 17px; }
.prose h3 {
  margin: 18px 0 6px;
  font-size: 16px;
  font-weight: 600;
  letter-spacing: -0.01em;
  text-transform: none;
  color: var(--navy);
}
.prose p { margin: 0 0 10px; }
.prose ul, .prose ol { margin: 0 0 12px; padding-left: 1.2em; }
.prose li { margin: 4px 0; }
.prose strong { font-weight: 600; }
.prose code {
  font-family: "SF Mono", ui-monospace, Menlo, monospace;
  font-size: 0.92em;
}
.lede { color: var(--slate); max-width: 42rem; margin: 0 0 24px; }
.role {
  display: block;
  margin-top: 2px;
  color: var(--slate);
  font-size: 13px;
  font-weight: 500;
}
.turn { padding: 14px 0; border-top: 1px solid var(--line); }
.turn p { margin: 6px 0 0; }
.turn-meta { color: var(--navy); font-weight: 600; }
.hint {
  margin: 0 0 16px;
  padding: 12px 16px;
  border-left: 3px solid var(--blue);
  background: var(--cloud);
  border-radius: 0 8px 8px 0;
}
.card {
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  padding: 16px;
}
.card h2 {
  margin: 0 0 12px;
  font-size: 13px;
  font-weight: 600;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--navy);
}
.columns {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 16px;
  margin: 8px 0 16px;
}
.card { min-width: 0; }
.grid {
  display: grid;
  grid-template-columns: 1.1fr 0.9fr;
  gap: 16px;
  margin: 0 0 16px;
}
@media (max-width: 820px) {
  .columns, .grid { grid-template-columns: 1fr; }
  .mast { align-items: flex-start; }
  .mark { width: 72px; }
}
ol.items { margin: 0; padding: 0; list-style: none; counter-reset: item; }
ol.items li {
  counter-increment: item;
  position: relative;
  padding: 12px 0 12px 32px;
  border-top: 1px solid var(--line);
}
ol.items li:first-child { border-top: 0; padding-top: 0; }
ol.items li::before {
  content: counter(item);
  position: absolute;
  left: 0;
  top: 12px;
  width: 22px;
  height: 22px;
  border-radius: 8px;
  background: var(--cloud);
  color: var(--navy);
  font-size: 12px;
  font-weight: 600;
  display: grid;
  place-items: center;
}
ol.items li:first-child::before { top: 0; }
.item { font-weight: 600; }
.why { color: var(--slate); font-size: 14px; margin-top: 4px; }
.row-meta { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-top: 8px; }
.pill {
  font-family: "JetBrains Mono", "SF Mono", ui-monospace, Menlo, monospace;
  font-size: 11px;
  letter-spacing: 0.02em;
  padding: 2px 8px;
  border-radius: 8px;
  border: 1px solid var(--line);
  color: var(--slate);
}
.pill.fact { background: var(--cloud); color: var(--navy); border-color: transparent; }
.pill.missing { color: var(--warn); border-color: var(--warn); }
.src { color: var(--navy); font-size: 13px; text-decoration: none; border-bottom: 1px solid var(--cyan); }
.src:hover { color: var(--blue); }
.empty { color: var(--slate); margin: 0; }
.bars { display: flex; align-items: flex-end; gap: 8px; min-height: 96px; }
.bar-col {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  color: var(--slate);
  font-size: 12px;
  font-family: "JetBrains Mono", "SF Mono", ui-monospace, Menlo, monospace;
}
.bar {
  width: 100%;
  max-width: 28px;
  border-radius: 8px 8px 4px 4px;
  background: linear-gradient(180deg, var(--cyan), var(--blue));
}
.focus-row {
  display: grid;
  grid-template-columns: 7.5rem 1fr 2.5rem;
  gap: 8px;
  align-items: center;
  margin: 0 0 8px;
  font-size: 14px;
}
.focus-track { height: 8px; background: var(--cloud); border-radius: 8px; overflow: hidden; }
.focus-fill { height: 100%; background: var(--blue); border-radius: 8px; }
.focus-row em { font-style: normal; text-align: right; color: var(--slate); font-variant-numeric: tabular-nums; }
.sources { margin-top: 8px; }
.sources ul { margin: 0; padding: 0; list-style: none; }
.sources li {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 4px;
  padding: 12px 0;
  border-bottom: 1px solid var(--line);
}
.sources li .pill { margin-top: 4px; }
.sources a { color: var(--navy); font-weight: 600; text-decoration: none; }
.sources a:hover { color: var(--blue); }
.sources span { color: var(--slate); font-size: 14px; }
footer { margin-top: 24px; color: var(--slate); font-size: 13px; }
table { width: 100%; border-collapse: collapse; }
th, td {
  text-align: left;
  padding: 8px;
  border-bottom: 1px solid var(--line);
  vertical-align: top;
}
th {
  font-size: 12px;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--slate);
  font-weight: 600;
}
td.mono, th.mono { font-family: "JetBrains Mono", "SF Mono", ui-monospace, Menlo, monospace; font-size: 13px; }
a { color: var(--navy); }
a:hover { color: var(--blue); }
.note { color: var(--warn); }
.err { color: var(--err); }
button.delete {
  font: 13px/1 -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
  color: var(--err);
  background: transparent;
  border: 1px solid var(--err);
  border-radius: 8px;
  padding: 4px 10px;
  cursor: pointer;
}
button.delete:hover { background: var(--cloud); }
button.texty {
  font: 13px/1 -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
  color: var(--navy);
  background: transparent;
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 4px 10px;
  cursor: pointer;
  margin-left: 8px;
}
button.texty:hover { background: var(--cloud); }
.name-line { display: flex; align-items: center; gap: 8px; }
.name-edit {
  display: flex;
  gap: 8px;
  margin-top: 8px;
}
.name-edit[hidden] { display: none; }
.name-edit input {
  flex: 1;
  min-width: 0;
  font: 14px -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
  color: var(--ink);
  background: var(--canvas);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 4px 8px;
}
.name-edit button { margin-left: 0; }
.voice { padding: 16px 0; border-bottom: 1px solid var(--line); }
.voice audio, .player audio { width: 100%; margin-top: 8px; }
.player + .player { margin-top: 14px; }
.dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  margin-right: 6px;
  vertical-align: 1px;
}
/* The brief is a board. Library and transcript pages stay a reading column. */
.wrap:has(.dashboard) { max-width: 1160px; }
.dashboard {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr)) minmax(240px, 0.85fr);
  column-gap: 16px;
  row-gap: 16px;
  align-items: stretch;
}
.dashboard .dash-top,
.dashboard .hint,
.dashboard .meetings,
.dashboard > footer { grid-column: 1 / -1; }
.dashboard .overview { grid-column: 1 / 4; }
.dashboard .week,
.dashboard .focus-card { grid-column: 4; }
.dashboard .boards { display: contents; }
.dashboard .dash-top {
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  gap: 24px;
}
.dashboard .mast { margin: 0; }
.dashboard .when { margin: 8px 0 0; }
.dashboard .stats { display: flex; gap: 10px; flex: none; }
.dashboard .stat {
  min-width: 96px;
  padding: 10px 14px;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--card);
}
.dashboard .stat strong {
  display: block;
  font-size: 22px;
  line-height: 1.1;
  letter-spacing: -0.03em;
  color: var(--navy);
  font-variant-numeric: tabular-nums;
}
.dashboard .stat span {
  display: block;
  margin-top: 4px;
  font-size: 11px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--slate);
}
.dashboard .overview { max-width: none; margin: 0; font-size: 16px; }
.dashboard .focus-row { grid-template-columns: minmax(4.5rem, 38%) minmax(0, 1fr) 2.2rem; }
.dashboard .focus-row span { overflow-wrap: anywhere; }
.dashboard .meetings { background: transparent; border: 0; padding: 0; }
.dashboard .sources ul {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 12px;
}
.dashboard .sources li {
  margin: 0;
  padding: 12px 14px;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--card);
}
@media (max-width: 980px) {
  .dashboard { grid-template-columns: 1fr; }
  .dashboard .overview,
  .dashboard .week,
  .dashboard .focus-card,
  .dashboard .boards > section { grid-column: 1; }
  .dashboard .dash-top { flex-direction: column; align-items: flex-start; }
  .dashboard .stats { flex-wrap: wrap; }
}
"""


def html_escape(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


@lru_cache(maxsize=1)
def logo_data_uri() -> str:
    raw = _MARK.read_bytes()
    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def masthead(eyebrow: str, title: str) -> str:
    return (
        '<header class="mast">'
        f'<img class="mark" alt="" src="{logo_data_uri()}">'
        "<div>"
        '<p class="wordmark">Yarn</p>'
        f'<p class="eyebrow">{html_escape(eyebrow)}</p>'
        f"<h1>{html_escape(title)}</h1>"
        "</div></header>"
    )


def document(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>{html_escape(title)}</title>
<style>
{CSS}
</style>
</head>
<body>
<main class="wrap">
{body}
</main>
</body>
</html>
"""
