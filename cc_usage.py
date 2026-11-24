#!/usr/bin/env python3
"""Claude Code token usage, live, in a terminal of its own.

    python3 cc_usage.py                 # redraws every 2 s; Ctrl-C quits
    python3 cc_usage.py --once          # one screen of the Overview, then exit
    python3 cc_usage.py --page week     # start on another page
    python3 cc_usage.py --theme amber   # start with another theme (hero / dash / amber)

Pages (left / right): Overview, Today, Week, All time. The layout follows the
window's size: it is laid out for a quarter of the screen and shows more when wider.
The last three list the projects; up / down move, Enter opens a project into its
sessions, by title. t switches the theme (Hero, Dashboard, Amber); the one showing
when it closes (Ctrl-C, the window closed, a kill) opens next time. No other key does
anything, and nothing typed is shown or left for the shell.

Source: Claude Code's own transcripts, ~/.claude/projects/<project>/<session>.jsonl
(a session's subagents in <session>/subagents/*.jsonl). Every API response Claude Code
gets is written there with its `usage`; a session's title too (`agent-name`, else
`ai-title`; without either, the first line typed in it). After the first look only
what a file gained is read.

Its own files live in $CC_USAGE_DIR (default ~/cc-usage):
- ratelimits.json: the account's limit windows, "% used" and reset time, which the
  transcripts do not hold. The mod in limits-mod/ writes it; without it the Overview
  leaves the limits out.
- state.json: the theme it was last closed on.
- aliases.json (optional, by hand): {"old-project-folder": "new-name"}, for a project
  whose folder was renamed or copied, so its sessions are counted once, under one name.

What the numbers rest on:
- One response is written as several lines, each carrying its usage again, and the
  earlier lines carry a partial output count. Lines are merged by (message.id,
  requestId), keeping the one with the most output tokens; adding up every line
  counts most responses twice or more.
- Lines whose model is "<synthetic>" are Claude Code's own notes, not API calls.
- A call Claude Code does not write to a transcript is not counted.
- Costs are the public API list prices below: an estimate, not a bill.
- A limit's allowance is estimated as (used in the limit's window) / (% used). A limit
  is not a plain token count (models weigh differently), and use outside Claude Code
  (claude.ai) counts toward it too, so the estimate runs low then.
"""

import argparse
import datetime as dt
import json
import math
import os
import re
import select
import signal
import sys
import termios
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, fields
from pathlib import Path
from types import SimpleNamespace

from rich import box
from rich.align import Align
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

ROOT = Path.home() / ".claude" / "projects"
DATA = Path(os.environ.get("CC_USAGE_DIR") or Path.home() / "cc-usage")  # the mod reads the same variable
LIMITS = DATA / "ratelimits.json"  # written by the cc-usage-limits mod
STATE = DATA / "state.json"  # the theme it was last closed on
ACTIVE_MIN = 30  # a session is shown as active if it got a response in the last N minutes


def load_aliases():
    """aliases.json: {"old-project-folder": "new-name"}; missing or broken means none."""
    try:
        aliases = json.loads((DATA / "aliases.json").read_text())
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in aliases.items()} if isinstance(aliases, dict) else {}


ALIASES = load_aliases()  # old project folder name -> the name it goes under

# $ per million tokens: (input, output, cache read); a cache read of None is 0.1 x input.
# Cache writes cost 1.25 x input with the 5-minute TTL and 2 x input with the 1-hour TTL.
# Anthropic's public list prices as of November 2026; a model missing here is shown without a $.
PRICES = {
    "claude-fable-5-2": (8.00, 40.00, 0.20),
    "claude-mythos-5-2": (8.00, 40.00, 0.20),
    "claude-fable-5-1": (10.00, 50.00, 0.25),
    "claude-mythos-5-1": (10.00, 50.00, 0.25),
    "claude-fable-5": (10.00, 50.00, None),
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-opus-5": (5.00, 25.00, None),
    "claude-opus-4-8": (5.00, 25.00, None),
    "claude-opus-4-7": (5.00, 25.00, None),
    "claude-opus-4-6": (5.00, 25.00, None),
    "claude-sonnet-5-5": (2.00, 10.00, 0.10),
    "claude-sonnet-5": (2.00, 10.00, None),
    "claude-sonnet-4-6": (3.00, 15.00, None),
    "claude-haiku-5-5": (0.10, 0.50, None),
    "claude-haiku-4-5": (1.00, 5.00, None),
}
HAIKU_55_LONG = (0.50, 2.50)  # claude-haiku-5-5 when one prompt is over 100K tokens
CONTEXT = {"claude-haiku-4-5": 200_000}  # every other model in PRICES: 1M
WEB_SEARCH = 10 / 1000  # $ per search
FAST = 2.0  # fast mode costs twice the standard price on every model that has it

WEEKDAY = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
WEEK = dt.timedelta(days=7)
FIVE_HOURS = dt.timedelta(hours=5)  # the "Current session" window in /usage
PAGES = ["Overview", "Today", "Week", "All time"]
PAGE_ARGS = {"overview": 0, "today": 1, "week": 2, "all": 3}


def base_model(model):
    """claude-haiku-4-5-20251001 / claude-opus-5-5[1m] -> the PRICES key."""
    return re.sub(r"(\[.*\]|-\d{8})$", "", model)


@dataclass(slots=True)
class Rec:
    """One API response."""

    ts: dt.datetime
    project: str
    session: str
    sub: bool  # a subagent's call, not the session's main thread
    model: str
    speed: str
    inp: int  # input not read from or written to the cache
    cw5: int  # input written to the cache, 5-minute TTL
    cw1h: int  # input written to the cache, 1-hour TTL
    cr: int  # input read from the cache
    out: int  # output, thinking included
    ws: int  # web searches
    usd: float | None = None  # None: no price for this model

    @property
    def sent(self):
        """The whole prompt of this request."""
        return self.inp + self.cw5 + self.cw1h + self.cr


def cost(r):
    model = base_model(r.model)
    if model not in PRICES:
        return None
    inp, out, read = PRICES[model]
    if model == "claude-haiku-5-5" and r.sent > 100_000:
        inp, out = HAIKU_55_LONG
        read = None
    if read is None:
        read = inp * 0.1
    per_million = r.inp * inp + r.cw5 * inp * 1.25 + r.cw1h * inp * 2 + r.cr * read + r.out * out
    return (FAST if r.speed == "fast" else 1.0) * per_million / 1e6 + r.ws * WEB_SEARCH


def first_typed(d):
    """The text of a line the user typed, or None for tool results, command wrappers and notes."""
    if d.get("isMeta") or d.get("isSidechain"):
        return None
    c = (d.get("message") or {}).get("content")
    if isinstance(c, list):
        c = " ".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    if not isinstance(c, str):
        return None
    c = " ".join(c.split())
    return c if c and not c.startswith("<") else None


class Reader:
    """Keeps every response and session title from the transcripts; each poll reads only what files gained."""

    def __init__(self, root):
        self.root = root
        self.home = str(Path.home()).replace("/", "-") + "-"  # "/home/me" -> "-home-me-", the folders' prefix
        self.pos = {}  # path -> (inode, bytes already read)
        self.recs = {}  # (message id, request id) -> Rec
        self.names = {}  # session id -> agent-name
        self.ai_titles = {}  # session id -> ai-title
        self.first = {}  # session id -> the first line typed in it

    def title(self, session):
        return self.names.get(session) or self.ai_titles.get(session) or self.first.get(session) or session[:8]

    def project_of(self, folder):
        name = folder.removeprefix(self.home)
        if name.startswith("-tmp-"):  # a session started in a temporary folder
            return "tmp"
        for old, new in ALIASES.items():
            if name == old or name.startswith(old + "-"):
                return new + name[len(old):]
        return name

    def poll(self):
        for path in sorted(self.root.rglob("*.jsonl")):
            try:
                st = path.stat()
            except OSError:
                continue
            ino, done = self.pos.get(path, (st.st_ino, 0))
            if ino != st.st_ino or st.st_size < done:  # replaced or cut short: read it again
                done = 0
            if st.st_size > done:
                try:
                    with open(path, "rb") as fh:
                        fh.seek(done)
                        data = fh.read(st.st_size - done)
                except OSError:
                    continue
                parts = path.relative_to(self.root).parts
                project = self.project_of(parts[0])
                sub = len(parts) > 2
                session = parts[1] if sub else Path(parts[1]).stem
                whole = data.rfind(b"\n") + 1  # a line still being written waits for the next poll
                for line in data[:whole].split(b"\n"):
                    self._take(line, project, session, sub)
                done += whole
            self.pos[path] = (st.st_ino, done)

    def _take(self, line, project, session, sub):
        if b'"usage"' in line:
            self._usage(line, project, session, sub)
        elif sub:
            return
        elif b'"ai-title"' in line or b'"agent-name"' in line:
            try:
                d = json.loads(line)
            except ValueError:
                return
            if d.get("type") == "ai-title" and d.get("aiTitle"):
                self.ai_titles[session] = d["aiTitle"]
            elif d.get("type") == "agent-name" and d.get("agentName"):
                self.names[session] = d["agentName"]
        elif session not in self.first and b'"type":"user"' in line:
            try:
                d = json.loads(line)
            except ValueError:
                return
            text = first_typed(d) if d.get("type") == "user" else None
            if text:
                self.first[session] = text

    def _usage(self, line, project, session, sub):
        try:
            d = json.loads(line)
        except ValueError:
            return
        if not isinstance(d, dict) or d.get("type") != "assistant":
            return
        m = d.get("message") or {}
        u = m.get("usage") or {}
        model = m.get("model") or "?"
        if not u or model == "<synthetic>":
            return
        key = (m.get("id"), d.get("requestId"))
        if key == (None, None):
            key = d.get("uuid") or id(d)
        out = u.get("output_tokens") or 0
        old = self.recs.get(key)
        if old is not None and old.out >= out:
            return
        if old is not None:
            ts, project, session, sub = old.ts, old.project, old.session, old.sub
        else:
            try:
                ts = dt.datetime.fromisoformat(d["timestamp"]).astimezone()
            except (KeyError, TypeError, ValueError):
                ts = dt.datetime.now().astimezone()
        cw = u.get("cache_creation_input_tokens") or 0
        cw1h = min((u.get("cache_creation") or {}).get("ephemeral_1h_input_tokens") or 0, cw)
        r = Rec(
            ts=ts,
            project=project,
            session=session,
            sub=sub,
            model=model,
            speed=u.get("speed") or "standard",
            inp=u.get("input_tokens") or 0,
            cw5=cw - cw1h,
            cw1h=cw1h,
            cr=u.get("cache_read_input_tokens") or 0,
            out=out,
            ws=(u.get("server_tool_use") or {}).get("web_search_requests") or 0,
        )
        r.usd = cost(r)
        self.recs[key] = r


def read_limits(path, kind="seven_day"):
    """One window from the mod's file (seven_day = the week, five_hour = /usage's "Current session"):
    (% used, resets at, read at), or a reason."""
    try:
        reading = json.loads(path.read_text())
        window = next(w for w in reading["rateLimits"] if w.get("kind") == kind)
        return (
            float(window["percentUsed"]),
            dt.datetime.fromisoformat(window["resetsAt"]).astimezone(),
            dt.datetime.fromtimestamp(reading["at"] / 1000).astimezone(),
        )
    except FileNotFoundError:
        return "no reading yet"
    except (OSError, ValueError, KeyError, TypeError, StopIteration):
        return f"no {kind} reading"



# ---------------------------------------------------------------- numbers and marks

ACCENT = "#3987e5"  # bars, meters, the current tab: one hue, readable on dark and light terminals
ON_ACCENT = "#ffffff"
WARN = "#fab219"  # a meter past 70%
DANGER = "#d03b3b"  # a meter past 90%
AMBER, AMBER_DIM, CRT = "#ffb000", "#9a6a00", "#160f02"
BLUES = ["#184f95", "#256abf", "#2a78d6", "#3987e5", "#5598e7", "#6da7ec", "#86b6ef", "#9ec5f4"]  # dark -> light
EIGHTHS = " ▏▎▍▌▋▊▉"
VERT = " ▁▂▃▄▅▆▇█"

# The "future" figure font: three rows of box-drawing strokes.
FUTURE = {
    "0": ("┏━┓", "┃┃┃", "┗━┛"), "1": ("╺┓ ", " ┃ ", "╺┻╸"), "2": ("┏━┓", "┏━┛", "┗━╸"),
    "3": ("┏━┓", "╺━┫", "┗━┛"), "4": ("╻ ╻", "┗━┫", "  ╹"), "5": ("┏━╸", "┗━┓", "┗━┛"),
    "6": ("┏━┓", "┣━┓", "┗━┛"), "7": ("┏━┓", "  ┃", "  ╹"), "8": ("┏━┓", "┣━┫", "┗━┛"),
    "9": ("┏━┓", "┗━┫", "┗━┛"), "$": ("┏╋┓", "┗╋┓", "┗╋┛"), ".": (" ", " ", "╹"),
    ",": (" ", " ", "┛"), "~": ("   ", "╺━╸", "   "), " ": ("  ", "  ", "  "),
}


@dataclass(frozen=True)
class Look:
    """One theme's colours. `frame` paints the whole screen; None keeps the terminal's own."""

    accent: str
    on_accent: str
    warn: str
    danger: str
    muted: str  # secondary text
    fill: str  # meter glyphs: filled, track
    track: str
    frame: str | None
    caps: bool  # labels in capitals


THEMES = {
    "hero": Look(ACCENT, ON_ACCENT, WARN, DANGER, "dim", "━", "─", None, False),
    "dash": Look(ACCENT, ON_ACCENT, WARN, DANGER, "dim", "━", "─", None, False),
    "amber": Look(AMBER, CRT, "#ffe14d", "#ff5a1f", AMBER_DIM, "▓", "░", f"{AMBER} on {CRT}", True),
}
THEME_TITLES = {"hero": "Hero", "dash": "Dashboard", "amber": "Amber"}


def load_theme():
    try:
        theme = json.loads(STATE.read_text()).get("theme")
    except (OSError, ValueError, AttributeError):
        theme = None
    return theme if theme in THEMES else "hero"


def save_theme(theme):
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps({"theme": theme}) + "\n")
    except OSError:
        pass


def tok(n):
    n = round(n)
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1e3:.1f}k"
    if n < 1_000_000_000:
        return f"{n / 1e6:.1f}M"
    return f"{n / 1e9:.2f}B"


def money(x):
    if x >= 1000:
        return f"${x:,.0f}"
    if x >= 100:
        return f"${x:.0f}"
    return f"${x:.2f}"


def when(ts, time_only=False):
    return f"{ts:%H:%M}" if time_only else f"{ts:%m-%d %H:%M}"


def until(td):
    minutes = int(td.total_seconds() // 60)
    days, minutes = divmod(minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    return f"{days}d {hours}h" if days else f"{hours}h {minutes}m"


@dataclass
class Sum:
    n: float = 0
    inp: float = 0
    cw: float = 0
    cr: float = 0
    out: float = 0
    usd: float = 0.0
    unpriced: int = 0

    @classmethod
    def of(cls, recs):
        s = cls()
        for r in recs:
            s.add(r)
        return s

    def add(self, r):
        self.n += 1
        self.inp += r.inp
        self.cw += r.cw5 + r.cw1h
        self.cr += r.cr
        self.out += r.out
        if r.usd is None:
            self.unpriced += 1
        else:
            self.usd += r.usd

    @property
    def total(self):
        return self.inp + self.cw + self.cr + self.out

    def money(self):
        return money(self.usd) + ("*" if self.unpriced else "")

    def scaled(self, f):
        return Sum(*(getattr(self, k.name) * f for k in fields(self)[:6]), self.unpriced)

    def minus(self, other):
        return Sum(*(getattr(self, k.name) - getattr(other, k.name) for k in fields(self)[:6]), self.unpriced)


def bounds(now):
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return day0, day0 - dt.timedelta(days=day0.weekday())


def short_model(model):
    """claude-opus-5-5 -> Opus 5.5"""
    m = re.fullmatch(r"claude-([a-z]+)-(\d+)(?:-(\d+))?", base_model(model))
    if not m:
        return model
    name, major, minor = m.groups()
    return f"{name.capitalize()} {major}" + (f".{minor}" if minor else "")


def model_cell(rs):
    models = Counter(r.model for r in rs).most_common()
    return short_model(models[0][0]) + (f" +{len(models) - 1}" if len(models) > 1 else "")


def severity(frac, look):
    return look.accent if frac < 0.7 else look.warn if frac < 0.9 else look.danger


def meter(frac, width, look, mark=None):
    """A one-line meter: the fill coloured by severity, the track the same hue dimmed, `mark` as ┃."""
    frac = max(0.0, min(frac, 1.0))
    color = severity(frac, look)
    filled = round(frac * width)
    at = None if mark is None else min(int(max(0.0, min(mark, 1.0)) * width), width - 1)
    t = Text(no_wrap=True)
    for i in range(width):
        if i == at:
            t.append("┃", style="bold")
        elif i < filled:
            t.append(look.fill, style=color)
        else:
            t.append(look.track, style=f"dim {color}")
    return t


def bar(frac, width, style):
    """A bar of whole and eighth blocks; the colour goes on the blocks only, never on the cell's padding."""
    cells = max(0.0, min(frac, 1.0)) * width
    full = int(cells)
    part = round((cells - full) * 8)
    if part == 8:
        full, part = full + 1, 0
    t = Text(no_wrap=True)
    t.append("█" * full + (EIGHTHS[part] if part and full < width else ""), style=style)
    return t


def big(text, style):
    rows = ["", "", ""]
    for ch in text:
        glyph = FUTURE.get(ch, ("   ", " ? ", "   "))
        for i in range(3):
            rows[i] += glyph[i] + " "
    return Text("\n".join(r.rstrip() for r in rows), style=style, no_wrap=True)


def braille(values, width, height, ramp):
    """An area chart in braille dots: two values per cell, four dots per row; `ramp` colours rows top to bottom."""
    levels = height * 4
    lines = []
    for row in range(height):
        t = Text(no_wrap=True)
        below = (height - 1 - row) * 4
        for c in range(width):
            code = 0x2800
            for side, v in enumerate(values[2 * c:2 * c + 2]):
                bits = (6, 2, 1, 0) if side == 0 else (7, 5, 4, 3)  # bottom dot first
                for k in range(max(0, min(4, round(v * levels) - below))):
                    code |= 1 << bits[k]
            t.append(chr(code) if code != 0x2800 else " ", style=ramp[row] if code != 0x2800 else None)
        lines.append(t)
    return lines


def vbars(values, height, colw, style):
    """Vertical bars, one per value (0..1), drawn with eighth blocks."""
    lines = []
    for row in range(height):
        t = Text(no_wrap=True)
        for v in values:
            level = v * height - (height - 1 - row)
            ch = "█" if level >= 1 else VERT[int(level * 8)] if level > 0 else " "
            t.append(ch * colw, style=style)
            t.append(" ")
        lines.append(t)
    return lines


def grid2(left, right, width=None):
    g = Table.grid(expand=width is None)
    g.width = width
    g.add_column(no_wrap=True, ratio=1, overflow="ellipsis")
    g.add_column(no_wrap=True, justify="right")
    g.add_row(left, right)
    return g


def context_frac(rs):
    """How full the session's main thread's context is, 0..1, or None."""
    main = [r for r in rs if not r.sub]
    if not main:
        return None
    last = max(main, key=lambda r: r.ts)
    model = base_model(last.model)
    if model not in PRICES:
        return None
    return last.sent / CONTEXT.get(model, 1_000_000)


# ---------------------------------------------------------------- what every Overview shows


def limit_info(recs, now, kind="seven_day", span=WEEK):
    reading = read_limits(LIMITS, kind)
    if isinstance(reading, str):
        return reading
    pct, reset, read_at = reading
    if reset <= now:
        return f"the last reading ({when(read_at)}) is from a window that has reset"
    start = reset - span
    used = Sum.of(r for r in recs if r.ts >= start)
    allowance = Sum.of(r for r in recs if start <= r.ts <= read_at).scaled(100 / pct) if pct >= 1 else None
    return {
        "pct": pct,
        "reset": reset,
        "read_at": read_at,
        "gone": (now - start) / span,
        "allowance": allowance,
        "left": allowance.minus(used) if allowance else None,
    }


def active_sessions(recs, now):
    by_session = defaultdict(list)
    for r in recs:
        by_session[r.session].append(r)
    active = [(max(r.ts for r in rs), s, rs) for s, rs in by_session.items()]
    active = [a for a in active if now - a[0] <= dt.timedelta(minutes=ACTIVE_MIN)]
    return sorted(active, key=lambda a: a[0], reverse=True)


def gather(reader, now):
    recs = list(reader.recs.values())
    day0, week0 = bounds(now)
    week = Sum.of(r for r in recs if r.ts >= week0)
    elapsed = now - week0
    days = []
    for i in range(7):
        d = week0 + dt.timedelta(days=i)
        days.append((d, Sum.of(r for r in recs if r.ts.date() == d.date()), d > day0, d == day0))
    info = limit_info(recs, now)
    sinfo = limit_info(recs, now, "five_hour", FIVE_HOURS)
    return SimpleNamespace(sinfo=sinfo, slimit=sinfo if isinstance(sinfo, dict) else None, 
        now=now, recs=recs, reader=reader, week=week, days=days, info=info,
        today=Sum.of(r for r in recs if r.ts >= day0),
        proj=week.scaled(WEEK / elapsed) if elapsed >= dt.timedelta(hours=1) else None,
        limit=info if isinstance(info, dict) else None,
        active=[(reader.title(s), model_cell(rs), context_frac(rs), Sum.of(rs)) for _, s, rs in active_sessions(recs, now)],
    )


def kpis(d):
    left = d.limit["left"] if d.limit else None
    return [
        ("This week", d.week.money(), f"{tok(d.week.total)} tok"),
        ("By Sunday", "~" + d.proj.money() if d.proj else "—", f"~{tok(d.proj.total)} tok" if d.proj else "too early"),
        ("Limit left", left.money() if left else "—",
         f"{tok(left.total)} / {tok(d.limit['allowance'].total)} tok" if left else "no estimate"),
    ]


def limit_block(info, reason, label, look, width, now, window, wrap=lambda x: x):
    """Three lines for one limit window: its % used, a meter with ┃ at the share of the window gone, when it resets."""
    if not info:
        return [wrap(Text(f"{label}: {reason}", style=look.muted))]
    pct, gone, reset, left = info["pct"], info["gone"], info["reset"], info["left"]
    at = f"{WEEKDAY[reset.weekday()]} {reset:%H:%M}" if window == "week" else f"{reset:%H:%M}"
    detail = f"resets {at} · in {until(reset - now)}" + (f" · ~{left.money()} left" if left else "")
    return [wrap(grid2(Text(label, style=look.muted), Text(f"{pct:g}% used", style="bold"), width=width)),
            wrap(meter(pct / 100, width, look, gone)),
            wrap(grid2(Text(detail, style=look.muted), Text(f"┃ {gone:.0%} of the {window} gone", style=look.muted),
                       width=width))]


def amber_limit(label, info, reason, window, look, width, now):
    """The same in the Amber theme's words: one meter line, one detail line."""
    if not info:
        return [Text(f"> {label:<15} {str(reason).upper()}", style=AMBER_DIM, no_wrap=True, overflow="ellipsis")]
    pct, gone, reset, left = info["pct"], info["gone"], info["reset"], info["left"]
    line = Text(f"> {label:<15} [", style=AMBER, no_wrap=True)
    line.append_text(meter(pct / 100, max(10, width - 31), look, gone))
    line.append(f"] {pct:5.1f}%", style=f"bold {AMBER}")
    at = f"{WEEKDAY[reset.weekday()]} {reset:%H:%M}" if window == "week" else f"{reset:%H:%M}"
    detail = f"  resets {at} · in {until(reset - now)}   ·   ┃ {gone:.0%} of {window} elapsed"
    detail += f"   ·   ~{left.money()} left" if left else ""
    return [line, Text(detail.upper(), style=AMBER_DIM, no_wrap=True, overflow="ellipsis")]


def reset_text(d):
    reset = d.limit["reset"]
    return f"resets {WEEKDAY[reset.weekday()]} {reset:%H:%M} · in {until(reset - d.now)}"


def nice_ceiling(x):
    """The smallest round number (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8 times a power of ten) that is at least x:
    the chart's top, so its half is round too."""
    if x <= 0:
        return 1.0
    step = 10 ** math.floor(math.log10(x))
    return next(m * step for m in (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10) if x <= m * step + 1e-12)


def activity_chart(recs, now, width, height, look, hours=6, dark=False):
    """An area chart of $ per bucket over the last `hours`, with a $ axis on the left and clock times below.

    Returns (lines, bucket minutes, peak $). `width` and `height` count the plot and its axes."""
    label_w = 7
    plot_w = max(10, width - label_w - 1)
    rows = max(2, height - 2)
    n = plot_w * 2
    vals = activity(recs, now, n, hours)
    peak = max(vals)
    top = nice_ceiling(peak)
    # more is darker on a light background and brighter on a dark one, so the peaks stand out on both
    blues = BLUES[::-1] if dark else BLUES
    ramp = [blues[min(i, len(blues) - 1)] for i in range(rows)] if look.accent == ACCENT else [look.accent] * rows
    plot = braille([v / top for v in vals], plot_w, rows, ramp)
    ticks = {0: top, rows // 2: top * (1 - (rows // 2) / rows)}
    lines = []
    for r, row in enumerate(plot):
        t = Text(f"{money(ticks[r]):>{label_w - 1}} ┤" if r in ticks else " " * label_w + "│", style=look.muted, no_wrap=True)
        t.append_text(row)
        lines.append(t)
    start = now - dt.timedelta(hours=hours)
    first_hour = start.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)
    xs = []
    h = first_hour
    while h <= now:
        xs.append((min(plot_w - 1, int((h - start) / (now - start) * plot_w)), f"{h:%H:%M}"))
        h += dt.timedelta(hours=1)
    axis = list("─" * plot_w)
    labels = [" "] * (plot_w + 6)
    free = 0
    for x, label in xs:
        axis[x] = "┴"
        left = min(max(0, x - 2), plot_w - 5)  # a label near either end slides in rather than being cut
        if left >= free:
            labels[left:left + 5] = label
            free = left + 6
    if free <= plot_w - 3:
        labels[plot_w - 3:plot_w] = "now"
    lines.append(Text(f"{'$0':>{label_w - 1}} └" + "".join(axis), style=look.muted, no_wrap=True))
    lines.append(Text(" " * (label_w + 1) + "".join(labels).rstrip(), style=look.muted, no_wrap=True))
    return lines, hours * 60 / n, peak


def activity(recs, now, n, hours):
    """$ per bucket over the last `hours`, `n` buckets, oldest first."""
    span = dt.timedelta(hours=hours)
    start = now - span
    vals = [0.0] * n
    for r in recs:
        if r.ts >= start and r.usd:
            vals[min(int((r.ts - start) / span * n), n - 1)] += r.usd
    return vals


# ---------------------------------------------------------------- the three Overviews


def overview_hero(d, look, width, height):
    c = Align.center
    mw = max(30, min(84, width - 16))
    parts = [c(Text("T O D A Y", style=look.muted)), "", c(big(d.today.money(), f"bold {look.accent}")), "",
             c(Text(f"{tok(d.today.total)} tokens   ·   {round(d.today.n)} requests   ·   {len(d.active)} active now",
                    style=look.muted)), "", ""]
    parts += limit_block(d.limit, d.info, "weekly limit", look, mw, d.now, "week", c)
    stats = Table.grid(padding=(0, 4))
    for _ in range(5):
        stats.add_column(justify="center", no_wrap=True)
    cells = []
    for label, value, sub in kpis(d):
        t = Text(label.lower() + "\n", style=look.muted)
        t.append(value + "\n", style="bold")
        t.append(sub, style=look.muted)
        cells += [t, Text("│\n│\n│", style=look.muted)]
    stats.add_row(*cells[:-1])
    parts += ["", "", c(stats)]
    session = limit_block(d.slimit, d.sinfo, "current session · 5-hour limit", look, mw, d.now, "5 h", c)
    parts[7:7] = [*session, "", ""]  # after the today line
    room = height - 26  # what is left for the sessions
    if d.active and room >= 3:
        g = Table.grid(padding=(0, 3))
        g.width = mw
        g.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        for _ in range(3):
            g.add_column(no_wrap=True, justify="right")
        for title, _, frac, s in d.active[:room - 2]:
            g.add_row(Text(title, style=look.muted), meter(frac or 0, 12, look), f"{(frac or 0):.0%}", s.money())
        parts += ["", "", c(g)]
    return [Align.center(Group(*parts), vertical="middle", height=height)]


def overview_dash(d, look, width, height):
    side = min(40, max(30, width // 3))
    gh = 6
    gw = max(18, width - side - 6)
    lines, bucket, peak = activity_chart(d.recs, d.now, gw, gh + 2, look, dark=d.dark)
    title = lambda s: Text(f" {s} ", style=f"bold {look.accent}")
    panel = lambda body, name, h: Panel(body, title=title(name), title_align="left", box=box.ROUNDED,
                                         border_style=look.accent, height=h)
    graph = Group(*lines)
    graph_title = (f"activity · $ spent per {bucket:.1f} min · peak {money(peak)}" if gw >= 60
                   else f"$ per {bucket:.1f} min · peak {money(peak)}")

    st = Table.grid(expand=True, padding=(0, 1))
    st.add_column(no_wrap=True, style=look.muted)
    st.add_column(no_wrap=True, justify="right", style="bold")
    st.add_column(no_wrap=True, justify="right", style=look.muted, overflow="ellipsis")
    st.add_row("today", d.today.money(), f"{tok(d.today.total)} tok")
    for label, value, sub in kpis(d):
        st.add_row(label.lower(), value, sub.split(" / ")[0])
    budget = [st, ""]
    if d.limit:
        pct = d.limit["pct"]
        budget += [grid2(Text("weekly limit", style=look.muted), Text(f"{pct:g}%", style="bold")),
                   meter(pct / 100, side - 4, look, d.limit["gone"]), Text(reset_text(d), style=look.muted)]
    else:
        budget.append(Text(f"weekly limit: {d.info}", style=look.muted))

    bottom = max(6, height - (gh + 4))
    ses = Table.grid(expand=True, padding=(0, 1))
    ses.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
    ses.add_column(no_wrap=True, style=look.muted)
    ses.add_column(no_wrap=True)
    ses.add_column(no_wrap=True, justify="right")
    ses.add_column(no_wrap=True, justify="right")
    cur_h = 5
    ses_h = max(3, bottom - cur_h)
    for t, model, frac, s in d.active[:ses_h - 2]:
        ses.add_row(t, model, meter(frac or 0, 10, look), f"{(frac or 0):.0%}", s.money())
    if not d.active:
        ses.add_row(Text("none", style=look.muted), "", "", "", "")
    inner = width - side - 6
    left_bottom = Group(panel(Group(*limit_block(d.slimit, d.sinfo, "5-hour limit", look, inner, d.now, "5 h")),
                              "current session", cur_h),
                        panel(ses, f"sessions · last {ACTIVE_MIN} min", ses_h))

    top_usd = max(s.usd for _, s, _, _ in d.days) or 1
    bars = vbars([0 if fut else s.usd / top_usd for _, s, fut, _ in d.days], max(2, bottom - 5), 3, look.accent)
    labels = Text(no_wrap=True)
    for day, _, _, today in d.days:
        labels.append(WEEKDAY[day.weekday()] + " ", style="bold" if today else look.muted)
    today_usd = Text(f"today {d.today.money()}  ·  week {d.week.money()}", style=look.muted)

    g = Table.grid(expand=True)
    g.add_column(ratio=1)
    g.add_column(width=side + 2)
    g.add_row(panel(graph, graph_title, gh + 4), panel(Group(*budget), "budget", gh + 4))
    g.add_row(left_bottom, panel(Group(*bars, labels, today_usd), "week", bottom))
    return [g]


def overview_amber(d, look, width, height):
    hot = f"bold {AMBER}"

    def leader(label, value, sub=""):
        t = Text(no_wrap=True, overflow="ellipsis")
        t.append("> " + label.upper() + " ", style=AMBER)
        t.append("." * max(2, 30 - len(label)), style=AMBER_DIM)
        t.append(f" {value:>9}", style=hot)
        t.append(f"   {sub.upper()}", style=AMBER_DIM)
        return t

    parts = ["", leader("Today", d.today.money(), f"{tok(d.today.total)} tok · {round(d.today.n)} req")]
    parts += [leader(*k) for k in kpis(d)]
    parts += ["", *amber_limit("CURRENT SESSION", d.slimit, d.sinfo, "5 h", look, width, d.now),
              "", *amber_limit("WEEKLY LIMIT", d.limit, d.info, "week", look, width, d.now)]
    parts += ["", Text("> ACTIVE PROCESSES", style=AMBER)]
    room = max(1, height - 20)
    for n, (title, model, frac, s) in enumerate(d.active[:room], 1):
        name = Text(title, style=AMBER)
        name.truncate(max(10, width - 52), overflow="ellipsis", pad=True)
        t = Text(f"  {n:02d} ", style=AMBER_DIM, no_wrap=True)
        t.append_text(name)
        t.append(f" {model.upper():<10} CTX ", style=AMBER_DIM)
        t.append_text(meter(frac or 0, 10, look))
        t.append(f" {(frac or 0):4.0%}  {s.money():>8}", style=hot)
        parts.append(t)
    if not d.active:
        parts.append(Text("  NONE", style=AMBER_DIM))
    top = max(s.usd for _, s, _, _ in d.days) or 1
    week = Text("> WEEK  ", style=AMBER, no_wrap=True)
    for day, s, fut, today in d.days:
        week.append(WEEKDAY[day.weekday()].upper() + " ", style=hot if today else AMBER_DIM)
        week.append("·" if fut else VERT[max(1, round(s.usd / top * 8))], style=AMBER)
        week.append("   ")
    parts += ["", week, "", Text("READY. ", style=AMBER) + Text("█", style=f"blink {AMBER}")]
    return parts


OVERVIEWS = {"hero": overview_hero, "dash": overview_dash, "amber": overview_amber}


# ---------------------------------------------------------------- the project pages


class UI:
    """Which page and theme are up, which row is picked, which projects are open."""

    def __init__(self, page=0, theme="hero"):
        self.page = page
        self.theme = theme
        self.open = set()  # projects opened into their sessions, on every list page
        self.picked = {}  # page -> the picked row's key
        self.top = {}  # page -> index of the first row on screen
        self.rows = {}  # page -> row keys as last drawn
        self.view = 10  # rows that fit on screen, as last drawn
        self.background = None  # the terminal's own background, (r, g, b), when it said
        self.live = False  # --once draws no picked row

    def pick_style(self):
        """(row style, share-bar colour) of the picked row."""
        if self.theme == "amber":
            return f"bold {CRT} on {AMBER}", CRT
        if self.background is None:
            return f"bold {ON_ACCENT} on {ACCENT}", ON_ACCENT
        accent = [int(ACCENT[i:i + 2], 16) for i in (1, 3, 5)]
        tint = "".join(f"{round(b + (a - b) * 0.22):02x}" for b, a in zip(self.background, accent))
        return f"on #{tint}", ACCENT

    def index(self):
        rows = self.rows.get(self.page) or []
        key = self.picked.get(self.page)
        return rows.index(key) if key in rows else 0

    def key(self, k):
        if k in ("left", "right"):
            self.page = (self.page + (1 if k == "right" else -1)) % len(PAGES)
            return
        if k == "theme":
            names = list(THEMES)
            self.theme = names[(names.index(self.theme) + 1) % len(names)]
            save_theme(self.theme)
            return
        rows = self.rows.get(self.page)
        if not rows or self.page == 0:
            return
        i = self.index()
        if k == "enter":
            kind, project, _ = rows[i]
            if kind == "p":
                self.open ^= {project}
            return
        i = {"up": i - 1, "down": i + 1, "pgup": i - self.view, "pgdn": i + self.view,
             "home": 0, "end": len(rows) - 1}.get(k, i)
        self.picked[self.page] = rows[max(0, min(i, len(rows) - 1))]


COLUMNS = {  # key: (header, column options)
    "name": ("Project / session", {"ratio": 1, "min_width": 14, "overflow": "ellipsis"}),
    "id": ("ID", {}),
    "model": ("Model", {}),
    "first": ("First", {}),
    "last": ("Last", {}),
    "req": ("Req", {"justify": "right"}),
    "inp": ("Input", {"justify": "right"}),
    "cw": ("Cache wr", {"justify": "right"}),
    "cr": ("Cache rd", {"justify": "right"}),
    "out": ("Output", {"justify": "right"}),
    "total": ("Tokens", {"justify": "right"}),
    "usd": ("Est. $", {"justify": "right"}),
    "share": ("", {"width": 8}),
}
# rows a project page uses besides its body rows: tabs and hint, the summary line or the panel's
# borders, column names and their rule, totals and their rule
CHROME = {"hero": 7, "dash": 8, "amber": 7}


def columns_for(width):
    cols = ["name"]
    if width >= 150:
        cols += ["id"]
    if width >= 118:
        cols += ["model", "first"]
    cols += ["last"]
    if width >= 92:
        cols += ["req"]
    if width >= 150:
        cols += ["inp", "cw", "cr"]
    if width >= 92:
        cols += ["out"]
    cols += ["total", "usd"]
    if width >= 72:
        cols += ["share"]
    return cols


def row_cells(s, rs, top_usd, time_only, bar_style):
    """The number cells of one row; `rs` None for the totals row, which has no times."""
    return {
        "first": when(min(r.ts for r in rs), time_only) if rs else "",
        "last": when(max(r.ts for r in rs), time_only) if rs else "",
        "req": str(round(s.n)),
        "inp": tok(s.inp),
        "cw": tok(s.cw),
        "cr": tok(s.cr),
        "out": tok(s.out),
        "total": tok(s.total),
        "usd": s.money(),
        "share": bar(s.usd / top_usd, 8, bar_style) if top_usd else "",
    }


def project_page(reader, ui, look, now, width, height):
    day0, week0 = bounds(now)
    lo, scope = {
        1: (day0, f"Today · {WEEKDAY[day0.weekday()]} {day0:%m-%d}"),
        2: (week0, f"This week · from Mon {week0:%m-%d}"),
        3: (None, "All time"),
    }[ui.page]
    tree = defaultdict(lambda: defaultdict(list))
    for r in reader.recs.values():
        if lo is None or r.ts >= lo:
            tree[r.project][r.session].append(r)
    psum = {p: Sum.of(r for rs in ss.values() for r in rs) for p, ss in tree.items()}
    total = Sum.of(r for ss in tree.values() for rs in ss.values() for r in rs)
    top_usd = max((s.usd for s in psum.values()), default=0)
    rows = []
    for p in sorted(tree, key=lambda p: psum[p].usd, reverse=True):
        rows.append(("p", p, None))
        if p in ui.open:
            for s in sorted(tree[p], key=lambda s: max(r.ts for r in tree[p][s]), reverse=True):
                rows.append(("s", p, s))
    ui.rows[ui.page] = rows
    ui.view = view = max(3, height + 2 - CHROME[ui.theme])  # height here is the body's: tabs and hint already out
    i = ui.index()
    top = ui.top.get(ui.page, 0)
    top = min(max(top, i - view + 1), i)
    top = max(0, min(top, len(rows) - view))
    ui.top[ui.page] = top
    shown = range(top, min(top + view, len(rows)))

    summary = Text(" " + (scope.upper() if look.caps else scope), style="bold")
    summary.append(f"   {len(tree)} project{'s' if len(tree) != 1 else ''} · {total.money()} · {tok(total.total)} tok ",
                   style=look.muted)
    scroll = Text(f" {top + 1}–{shown.stop} of {len(rows)} ", style=look.muted) if len(rows) > view else Text("")

    pick, pick_bar = ui.pick_style() if ui.live else (None, look.accent)
    cols = columns_for(width - (2 if ui.theme == "dash" else 0))
    footer = {"name": "TOTAL" if look.caps else "Total", **row_cells(total, None, 0, True, look.accent)}
    t = Table(box=box.SIMPLE_HEAD, show_edge=False, show_footer=True, expand=True, pad_edge=True,
              header_style=look.muted if ui.theme == "hero" else f"bold {look.accent}", footer_style="bold",
              border_style=look.muted)
    for c in cols:
        header, opts = COLUMNS[c]
        t.add_column(header.upper() if look.caps else header, footer=footer.get(c, ""), no_wrap=True, **opts)
    time_only = ui.page == 1
    for idx in shown:
        kind, p, s = rows[idx]
        picked = pick if idx == i else None
        bar_style = pick_bar if picked else look.accent
        if kind == "p":
            rs = [r for ss in tree[p].values() for r in ss]
            name = Text(("▾ " if p in ui.open else "▸ ") + (p.upper() if look.caps else p), style="bold")
            name.append(f"  {len(tree[p])}", style=None if picked else look.muted)
            cells = {"name": name, "id": "", "model": "", **row_cells(psum[p], rs, top_usd, time_only, bar_style)}
        else:
            rs = tree[p][s]
            last_child = idx + 1 >= len(rows) or rows[idx + 1][0] == "p"
            name = Text("  └ " if last_child else "  ├ ", style=None if picked else look.muted)
            name.append(reader.title(s))
            dim = None if picked else look.muted
            cells = {"name": name, "id": Text(s[:8], style=dim), "model": Text(model_cell(rs), style=dim),
                     **row_cells(Sum.of(rs), rs, top_usd, time_only, bar_style)}
        t.add_row(*(cells[c] for c in cols), style=picked)
    if not rows:
        t.add_row(*([Text("nothing yet", style=look.muted)] + [""] * (len(cols) - 1)))
    if ui.theme == "dash":
        return [Panel(t, title=summary, title_align="left", subtitle=scroll, subtitle_align="right", box=box.ROUNDED,
                      border_style=look.accent, height=height)]
    return [grid2(summary, scroll), t]


# ---------------------------------------------------------------- the screen


def tabs(ui, look, now):
    t = Text(no_wrap=True)
    for n, name in enumerate(PAGES):
        label = f" {name.upper() if look.caps else name} "
        t.append(label, style=f"bold {look.on_accent} on {look.accent}" if n == ui.page else look.muted)
        t.append(" ")
    right = THEME_TITLES[ui.theme].upper() if look.caps else THEME_TITLES[ui.theme]
    return grid2(t, Text(f"{right} · {now:%H:%M:%S} ", style=look.muted))


def render(reader, ui, now, width, height):
    """The whole screen: tabs, the page, the key hint on the last line."""
    look = THEMES[ui.theme]
    if look.frame:  # a double border and one column of padding each side, all of it painted
        width, height = width - 4, height - 2
    if ui.page == 0:
        d = gather(reader, now)
        bg = ui.background
        d.dark = bg is not None and 0.2126 * bg[0] + 0.7152 * bg[1] + 0.0722 * bg[2] < 128  # unknown: light
        body = OVERVIEWS[ui.theme](d, look, width, height - 2)
        keys = "←/→ page   t theme   ^C quit"
    else:
        body = project_page(reader, ui, look, now, width, height - 2)
        keys = "←/→ page   ↑/↓ move   ⏎ open/close   t theme   ^C quit"
    hint = Text(" " + (keys.upper() if look.caps else keys), style=look.muted, no_wrap=True, overflow="ellipsis")
    unpriced = sorted({r.model for r in reader.recs.values() if r.usd is None})
    if unpriced:
        hint.append(f"   * no price: {', '.join(unpriced)}", style=look.muted)
    screen = Layout()
    screen.split_column(Layout(tabs(ui, look, now), size=1), Layout(Group(*body)), Layout(hint, size=1))
    if look.frame:
        return Panel(screen, box=box.DOUBLE, border_style=look.accent, style=look.frame, padding=(0, 1),
                     height=height + 2)
    return screen


KEYS = {
    b"\x1b[A": "up", b"\x1b[B": "down", b"\x1b[C": "right", b"\x1b[D": "left",
    b"\x1bOA": "up", b"\x1bOB": "down", b"\x1bOC": "right", b"\x1bOD": "left",
    b"\x1b[5~": "pgup", b"\x1b[6~": "pgdn",
    b"\x1b[H": "home", b"\x1b[F": "end", b"\x1bOH": "home", b"\x1bOF": "end", b"\x1b[1~": "home", b"\x1b[4~": "end",
    b"\r": "enter", b"\n": "enter", b" ": "enter", b"t": "theme", b"T": "theme",
}


def parse_keys(data):
    """Bytes read from the terminal -> key names; anything else is dropped."""
    keys, i = [], 0
    while i < len(data):
        for seq, name in KEYS.items():
            if data.startswith(seq, i):
                keys.append(name)
                i += len(seq)
                break
        else:
            if data[i] == 0x1B and data[i + 1:i + 2] in (b"[", b"]"):  # another escape sequence: skip it whole
                j = i + 2
                while j < len(data) and not 0x40 <= data[j] <= 0x7E and data[j] != 0x07:
                    j += 1
                i = j + 1
            else:
                i += 1
    return keys


def terminal_background(fd):
    """The terminal's background colour as (r, g, b), asked with OSC 11; None if it does not answer in 0.3 s."""
    data = b""
    try:
        os.write(sys.stdout.fileno(), b"\x1b]11;?\x07")
        end = time.monotonic() + 0.3
        while time.monotonic() < end and not data.endswith((b"\x07", b"\x1b\\")):
            if select.select([fd], [], [], max(0.0, end - time.monotonic()))[0]:
                data += os.read(fd, 64)
    except OSError:
        return None
    m = re.search(rb"rgb:([0-9a-fA-F]{2,4})/([0-9a-fA-F]{2,4})/([0-9a-fA-F]{2,4})", data)
    return tuple(int(x[:2], 16) for x in m.groups()) if m else None


@contextmanager
def raw_keys():
    """Keys reach the program one by one and are never shown; Ctrl-C still quits. Yields the fd, or None."""
    if not sys.stdin.isatty():
        yield None
        return
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    raw = termios.tcgetattr(fd)
    raw[3] &= ~(termios.ECHO | termios.ICANON)
    raw[6][termios.VMIN] = 1
    raw[6][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSANOW, raw)
    try:
        yield fd
    finally:
        termios.tcflush(fd, termios.TCIFLUSH)
        termios.tcsetattr(fd, termios.TCSANOW, old)


def main():
    ap = argparse.ArgumentParser(description="Claude Code token usage, live.")
    ap.add_argument("--once", action="store_true", help="print one screen and exit")
    ap.add_argument("--page", choices=PAGE_ARGS, default="overview", help="the page to start on")
    ap.add_argument("--theme", choices=THEMES, help="the theme to start with (default: the one last picked with t)")
    ap.add_argument("--every", type=float, default=2.0, help="seconds between reads (default 2)")
    ap.add_argument("--root", type=Path, default=ROOT, help="transcripts folder (default ~/.claude/projects)")
    args = ap.parse_args()

    reader = Reader(args.root)
    reader.poll()
    ui = UI(PAGE_ARGS[args.page], args.theme or load_theme())
    console = Console()
    if args.once:
        console.print(render(reader, ui, dt.datetime.now().astimezone(), *console.size))
        return

    def screen():
        return render(reader, ui, dt.datetime.now().astimezone(), *console.size)

    def closed(signum, frame):  # the terminal closed (SIGHUP) or a kill (SIGTERM): leave the way Ctrl-C does
        raise KeyboardInterrupt

    signal.signal(signal.SIGHUP, closed)
    signal.signal(signal.SIGTERM, closed)
    try:
        with raw_keys() as fd:
            ui.background = terminal_background(fd) if fd is not None else None
            ui.live = True
            with Live(screen(), console=console, screen=True, auto_refresh=False) as live:
                next_read = time.monotonic() + args.every
                while True:
                    wait = max(0.0, next_read - time.monotonic())
                    if fd is None:
                        time.sleep(wait)
                    elif select.select([fd], [], [], wait)[0]:
                        for k in parse_keys(os.read(fd, 1024)):
                            ui.key(k)
                    if time.monotonic() >= next_read:
                        reader.poll()
                        next_read = time.monotonic() + args.every
                    live.update(screen(), refresh=True)
    except KeyboardInterrupt:
        pass
    finally:
        save_theme(ui.theme)  # the theme it was closed on is the one it opens with next time


if __name__ == "__main__":
    main()
