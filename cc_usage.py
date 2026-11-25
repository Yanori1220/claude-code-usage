#!/usr/bin/env python3
"""Claude Code token usage, live, in a terminal of its own.

    python3 cc_usage.py                 # redraws every 2 s; Ctrl-C quits
    python3 cc_usage.py --once          # one screen of the Overview, then exit
    python3 cc_usage.py --page week     # start on another page
    python3 cc_usage.py --theme amber   # start with another theme (hero / dash / amber)

Pages (left / right): Overview, Today, Week, All time, Spend, History. The layout
follows the window's size: it is laid out for a quarter of the screen and shows more
when wider. Today, Week and All time list the projects: up / down move, Enter opens a
project into its sessions, by title, and a session into its own page (Esc goes back);
s sorts them by cost or by last use. t switches the theme (Hero, Dashboard, Amber).
The theme and sort it closes on (Ctrl-C, the window closed, a kill) open next time.
No other key does anything, and nothing typed is shown or left for the shell.

Source: Claude Code's own transcripts, ~/.claude/projects/<project>/<session>.jsonl
(a session's subagents in <session>/subagents/*.jsonl). Every API response Claude Code
gets is written there with its `usage`; a session's title too (`agent-name`, else
`ai-title`; without either, the first line typed in it). After the first look only
what a file gained is read.

Its own files live in $CC_USAGE_DIR (default ~/cc-usage):
- ratelimits.json: the account's limit windows, "% used" and reset time, which the
  transcripts do not hold. The mod in limits-mod/ writes it; without it (or usage.json)
  the Overview leaves the limits out.
- usage.json: the same as Anthropic reports it to /usage, with the per-model weekly
  limits (Fable's) the mod never sees; written here with --usage-api, which asks with
  Claude Code's login token every 5 minutes.
- state.json: the theme and sort it was last closed on, whether --usage-api is on, the
  alerts already sent, and each limit's last reading and refill.
- aliases.json (optional, by hand): {"old/folder": "new-name"}, for a project whose folder
  was renamed or copied, so its sessions are counted once, under one name; a key can be
  the folder's name as Claude Code writes it, too ("old-folder").

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
  (claude.ai) counts toward it too, so the estimate runs low then. A limit refilled
  early (/limit-reset, a reset voucher) keeps its reset time while its % falls; from
  then on the window counts from the refill.
- A project is the folder its sessions started in (their lines' `cwd`), as a path under
  the home folder; a week runs Monday to Sunday.
"""

import argparse
import datetime as dt
import json
import math
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import termios
import textwrap
import threading
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, fields
from pathlib import Path
from types import SimpleNamespace

from rich import box
from rich.align import Align
from rich.cells import cell_len
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
USAGE = DATA / "usage.json"  # the plan's usage as /usage reads it, written here when --usage-api is on
STATE = DATA / "state.json"  # the theme it was last closed on, the alerts already sent, the limits' refills
USAGE_URL = "https://api.anthropic.com/api/oauth/usage?skip_spend=1"  # what Claude Code's /usage asks
USAGE_EVERY = 300  # seconds between asks
ACTIVE_MIN = 30  # a session is shown as active if it got a response in the last N minutes
CONTEXT_WARN = (400_000, 700_000)  # context sizes (tokens) a session is warned about, then alerted again


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
PAGES = ["Overview", "Today", "Week", "All time", "Spend", "History"]
PAGE_ARGS = {"overview": 0, "today": 1, "week": 2, "all": 3, "spend": 4, "history": 5}
SPEND, HISTORY = 4, 5
SORTS = ["cost", "recent"]  # the project lists' orders, switched with s


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
    parts: dict | None = None  # $ by what was paid for: input, cache write, cache read, output, web search

    @property
    def sent(self):
        """The whole prompt of this request."""
        return self.inp + self.cw5 + self.cw1h + self.cr


PAID_FOR = ["input", "cache write", "cache read", "output", "web search"]


def cost_parts(r):
    """$ by what was paid for, or None for a model with no price."""
    model = base_model(r.model)
    if model not in PRICES:
        return None
    inp, out, read = PRICES[model]
    if model == "claude-haiku-5-5" and r.sent > 100_000:
        inp, out = HAIKU_55_LONG
        read = None
    if read is None:
        read = inp * 0.1
    f = (FAST if r.speed == "fast" else 1.0) / 1e6
    return {"input": r.inp * inp * f, "cache write": (r.cw5 * 1.25 + r.cw1h * 2) * inp * f,
            "cache read": r.cr * read * f, "output": r.out * out * f, "web search": r.ws * WEB_SEARCH}


def cost(r):
    parts = cost_parts(r)
    return None if parts is None else sum(parts.values())


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
        self.where = {}  # session id -> the project its records go under, from the folder it started in
        self.waiting = defaultdict(list)  # session id -> its records made before that folder was known

    def title(self, session):
        return self.names.get(session) or self.ai_titles.get(session) or self.first.get(session) or session[:8]

    def project_of(self, folder, cwd=None):
        """The name a session's records go under: the folder it started in (its first line's `cwd`), under the
        home folder, as a path ("work/app"). Without one, the project folder's name, in which Claude Code wrote
        every "/" and "_" as "-". aliases.json renames either: a key is a path or such a folder name."""
        if folder.startswith("-tmp-") or (cwd or "").startswith("/tmp/"):  # a session started in a temporary folder
            return "tmp"
        if cwd:
            home = str(Path.home())
            name = "~" if cwd == home else cwd[len(home) + 1:] if cwd.startswith(home + "/") else cwd
            parts = name.split("/")
            for i in range(len(parts), 0, -1):  # the most leading folders an alias names
                head = "/".join(parts[:i])
                new = ALIASES.get(head) or ALIASES.get(re.sub(r"[^A-Za-z0-9]", "-", head))
                if new:
                    return "/".join([new, *parts[i:]])
            return name
        name = folder.removeprefix(self.home)
        for old, new in ALIASES.items():
            if name == old or name.startswith(old + "-"):
                return new + name[len(old):]
        return name

    def poll(self):
        # as strings, a session's own file (<id>.jsonl) comes before its subagents' (<id>/subagents/), so the
        # folder the session started in is known before its subagents' records are made
        for path in sorted(self.root.rglob("*.jsonl"), key=str):
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
                sub = len(parts) > 2
                session = parts[1] if sub else Path(parts[1]).stem
                whole = data.rfind(b"\n") + 1  # a line still being written waits for the next poll
                for line in data[:whole].split(b"\n"):
                    self._take(line, parts[0], session, sub)
                done += whole
            self.pos[path] = (st.st_ino, done)

    def _take(self, line, folder, session, sub):
        if b'"usage"' in line:
            self._usage(line, folder, session, sub)
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

    def _usage(self, line, folder, session, sub):
        try:
            d = json.loads(line)
        except ValueError:
            return
        if not isinstance(d, dict) or d.get("type") != "assistant":
            return
        # the folder a session started in, from its main thread (a subagent may work in a worktree of its own)
        if session not in self.where and not sub and isinstance(d.get("cwd"), str) and d["cwd"]:
            self.where[session] = self.project_of(folder, d["cwd"])
            for r in self.waiting.pop(session, []):  # records made before it was known go under it too
                r.project = self.where[session]
        project = self.where.get(session) or self.project_of(folder)
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
        r.parts = cost_parts(r)
        r.usd = None if r.parts is None else sum(r.parts.values())
        self.recs[key] = r
        if session not in self.where:
            self.waiting[session].append(r)


def iso_minute(text):
    """An ISO 8601 time ("Z" too) to the nearest minute: the usage endpoint's carry stray milliseconds, and
    "resets 10:59" for 11:00 would read wrong."""
    t = dt.datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone()
    return (t + dt.timedelta(seconds=30)).replace(second=0, microsecond=0)


def read_limits(kind="seven_day", paths=None):
    """One window's newest reading, from the mod's file or the usage endpoint's (seven_day = the week, five_hour =
    /usage's "Current session", seven_day_fable = "Current week (Fable)"): (% used, resets at, read at), or a reason."""
    best, reason = None, "no reading yet"
    for path in paths or (LIMITS, USAGE):
        try:
            reading = json.loads(path.read_text())
            window = next(w for w in reading["rateLimits"] if w.get("kind") == kind)
            got = (float(window["percentUsed"]), iso_minute(window["resetsAt"]),
                   dt.datetime.fromtimestamp(reading["at"] / 1000).astimezone())
        except FileNotFoundError:
            continue
        except (OSError, ValueError, KeyError, TypeError, AttributeError, StopIteration):
            reason = f"no {kind} reading"
            continue
        if best is None or got[2] > best[2]:
            best = got
    return best or reason


def scoped_limits():
    """The plan's per-model weekly limits, as the usage endpoint last reported them: [(kind, model name)],
    e.g. [("seven_day_fable", "Fable")]."""
    try:
        return [(w["kind"], w["label"]) for w in json.loads(USAGE.read_text())["rateLimits"] if w.get("label")]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def login_token():
    """Claude Code's login token, from its credentials file: (token, None), or (None, why not)."""
    folder = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    try:
        login = json.loads((folder / ".credentials.json").read_text())["claudeAiOauth"]
        token, expires = login["accessToken"], login.get("expiresAt")
    except (OSError, ValueError, KeyError, TypeError):
        return None, "no Claude Code login found"
    if isinstance(expires, (int, float)) and expires / 1000 < time.time() + 60:
        return None, "Claude Code's login has expired; it renews it when it next runs"
    return token, None


def ask_usage():
    """Ask Anthropic for the plan's usage, as /usage does, with Claude Code's login token, and write its windows to
    usage.json. The token goes to api.anthropic.com and nowhere else, and is never renewed here: renewing it
    would replace the one Claude Code holds. Returns None, or why it could not."""
    token, why = login_token()
    if token is None:
        return why
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20",
        "Content-Type": "application/json", "User-Agent": "cc-usage"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.load(resp)
    except urllib.error.HTTPError as e:
        return f"the usage request was refused ({e.code})"
    except (OSError, ValueError):
        return "the usage request failed"
    windows = []
    for kind in ("five_hour", "seven_day"):
        w = body.get(kind)
        if isinstance(w, dict) and w.get("utilization") is not None and w.get("resets_at"):
            windows.append({"kind": kind, "percentUsed": w["utilization"], "resetsAt": w["resets_at"]})
    for row in body.get("limits") or []:  # the per-model weekly limits: "Current week (Fable)"
        name = ((row.get("scope") or {}).get("model") or {}).get("display_name") if isinstance(row, dict) else None
        if name and row.get("kind") == "weekly_scoped" and row.get("percent") is not None and row.get("resets_at"):
            windows.append({"kind": "seven_day_" + re.sub(r"\W+", "_", name.lower()), "label": name,
                            "percentUsed": row["percent"], "resetsAt": row["resets_at"]})
    try:
        part = USAGE.with_suffix(".part")
        part.write_text(json.dumps({"at": round(time.time() * 1000), "rateLimits": windows}) + "\n")
        part.replace(USAGE)
    except OSError:
        return f"could not write {USAGE}"
    return None


class UsageAsker(threading.Thread):
    """Asks for the plan's usage every USAGE_EVERY seconds, in the background, so a slow answer never holds the
    screen up; after a refusal for asking too often (429) it waits twice as long each time, up to half an hour."""

    def __init__(self):
        super().__init__(daemon=True)
        self.why = None  # why the last ask failed, or None

    def run(self):
        wait = USAGE_EVERY
        while True:
            self.why = ask_usage()
            wait = min(wait * 2, 1800) if self.why and "(429)" in self.why else USAGE_EVERY
            time.sleep(wait)



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


def load_state():
    try:
        state = json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def save_state(**changes):
    state = load_state()
    state.update(changes)
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(state) + "\n")
    except OSError:
        pass


def load_theme():
    theme = load_state().get("theme")
    return theme if theme in THEMES else "hero"


def save_theme(theme):
    save_state(theme=theme)


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
    return f"{days}d {hours}h" if days else f"{hours}h {minutes}m" if hours else f"{minutes}m"


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


def mark_x(mark, width):
    """The column of a meter's ┃ mark."""
    return min(int(max(0.0, min(mark, 1.0)) * width), width - 1)


def time_caption(left, x, width, gone, window, look, caps=False):
    """The line under a meter: `left`, then the label of the ┃ mark set right under it (column x), on
    whichever side has room, so its ┃ carries the mark's line down and the mark reads as "now"."""
    up = (lambda v: v.upper()) if caps else (lambda v: v)
    width_left = cell_len(left)
    t = Text(left, style=look.muted, no_wrap=True)
    for core in (up(f"now · {gone:.0%} of the {window} gone"), up(f"now · {gone:.0%}")):
        if x >= width_left + 2 and x + len(core) + 2 <= width:  # after the mark
            t.append(" " * (x - width_left))
            t.append("┃", style="bold")
            t.append(" " + core, style=look.muted)
            break
        if x + 1 - (len(core) + 2) >= width_left + 2:  # before it
            t.append(" " * (x + 1 - (len(core) + 2) - width_left))
            t.append(core + " ", style=look.muted)
            t.append("┃", style="bold")
            break
    else:  # no room beside the mark: the label at the end of the line
        t.append("   " + up(f"┃ now · {gone:.0%} of the {window} gone"), style=look.muted)
    t.truncate(width, overflow="ellipsis", pad=True)
    return t


def meter(frac, width, look, mark=None):
    """A one-line meter: the fill coloured by severity, the track the same hue dimmed, `mark` as ┃."""
    frac = max(0.0, min(frac, 1.0))
    color = severity(frac, look)
    filled = round(frac * width)
    at = None if mark is None else mark_x(mark, width)
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


REFILLS = {}  # kind -> its window's last refill (a /limit-reset, a reset voucher), as note_refills keeps it


def limit_windows():
    """Every limit a reading can come for: (kind, span, the model a per-model limit counts, label)."""
    return [("five_hour", FIVE_HOURS, None, "5-hour limit"), ("seven_day", WEEK, None, "Weekly limit")] + [
        (kind, WEEK, name, f"{name} limit") for kind, name in scoped_limits()]


def model_recs(recs, name):
    """The requests a per-model limit counts: those of the model it names ("Fable": Fable 5, Fable 5.2, ...)."""
    name = name.lower()
    return [r for r in recs if short_model(r.model).lower().startswith(name)]


def window_ts(reset):
    """A limit window's key: when it resets, in seconds."""
    return int(reset.timestamp())


def estimate(recs, start, pct, read_at, refill):
    """A window's allowance (the Sum of requests it takes to use all of it) and how many points of the window that
    rests on: what was used from the window's start, or its last refill, up to the reading, over the points the %
    moved meanwhile. After a refill the estimate from before it holds until a new one rests on 10 points."""
    base_at, base_pct = start, 0.0
    if refill:
        base_at, base_pct = dt.datetime.fromtimestamp(refill["at"]).astimezone(), refill["pct"]
    moved = pct - base_pct
    kept = (Sum(*refill["allowance"]), refill["basis"]) if refill and refill.get("allowance") else None
    if kept and moved < 10:
        return kept
    used = Sum.of(r for r in recs if base_at <= r.ts <= read_at)
    if moved >= 1 and used.usd > 0:  # nothing recorded (all of it used on claude.ai, say): no estimate, not $0
        return used.scaled(100 / moved), moved
    return kept or (None, moved)


def note_refills(recs, now):
    """Notice a limit refilled before it resets (a /limit-reset, a reset voucher): its % falls while its reset stays
    put. The % then counts only what came after the refill, so the estimates count from there too. A reading is
    compared only with the last one from the same file (the mod's, or the endpoint's, which may lag the other by
    a point or two), and a fall both see is one refill. Each window's last readings and refill are kept in
    state.json, so a refill made while this was not running is noticed at the next start."""
    recs = list(recs)
    seen = load_state().get("limits")
    seen = seen if isinstance(seen, dict) else {}
    kept = dict(seen)
    for kind, span, model, _ in limit_windows():
        for path in (LIMITS, USAGE):
            reading = read_limits(kind, (path,))
            if isinstance(reading, str) or reading[1] <= now:
                continue
            pct, reset, read_at = reading
            entry = kept.get(kind) if isinstance(kept.get(kind), dict) else {}
            if entry.get("reset") != window_ts(reset):  # a new window
                entry = {"reset": window_ts(reset), "seen": {}}
            last = (entry.get("seen") or {}).get(path.name)  # [% used, read at] from this file
            refill = entry.get("refill")
            # a fall of 3 points or more, not already taken for a refill since that last reading
            if last and pct <= last[0] - 3 and read_at.timestamp() > last[1] and not (refill and refill["at"] > last[1]):
                allowance, basis = estimate(model_recs(recs, model) if model else recs, reset - span, last[0],
                                            dt.datetime.fromtimestamp(last[1]).astimezone(), refill)
                refill = {"reset": window_ts(reset), "at": read_at.timestamp(), "pct": pct, "basis": basis,
                          "allowance": [getattr(allowance, f.name) for f in fields(Sum)[:6]] if allowance else None}
            kept[kind] = {**entry, "seen": {**(entry.get("seen") or {}), path.name: [pct, read_at.timestamp()]},
                          **({"refill": refill} if refill else {})}
    if kept != seen:
        save_state(limits=kept)
    REFILLS.clear()
    REFILLS.update({k: v["refill"] for k, v in kept.items() if isinstance(v, dict) and v.get("refill")})


def limit_info(recs, now, kind="seven_day", span=WEEK, model=None):
    """A limit window's reading and what the requests made in it say: its allowance, what is left, the pace, and
    when it runs out at that pace. `model` narrows the requests to that model's, for a per-model limit."""
    reading = read_limits(kind)
    if isinstance(reading, str):
        return reading
    pct, reset, read_at = reading
    if reset <= now:
        return f"the last reading ({when(read_at)}) is from a window that has reset"
    if model:
        recs = model_recs(recs, model)
    start = reset - span
    refill = REFILLS.get(kind)
    if refill and refill.get("reset") != window_ts(reset):
        refill = None
    since = dt.datetime.fromtimestamp(refill["at"]).astimezone() if refill else start  # what the % counts from
    allowance, basis = estimate(recs, start, pct, read_at, refill)
    # the pace: % per hour over the recent stretch (the last 30 min of a 5-hour window, the last
    # 24 h of the week), from $ spent against the allowance; without an allowance, the average since `since`
    recent = dt.timedelta(minutes=30) if span == FIVE_HOURS else dt.timedelta(hours=24)
    stretch = max(start, now - recent)
    hours = max((now - stretch).total_seconds() / 3600, 1 / 60)
    if allowance and allowance.usd > 0:
        rate = sum(r.usd or 0 for r in recs if r.ts >= stretch) / allowance.usd * 100 / hours
        pct_now = pct + sum(r.usd or 0 for r in recs if r.ts > read_at) / allowance.usd * 100  # and since the reading
    else:
        rate = (pct - (refill["pct"] if refill else 0)) / max((now - since).total_seconds() / 3600, 1 / 60)
        pct_now = pct
    # too early to tell: a window just begun, or so little used that a 1% step in the reading moves the
    # allowance by a tenth or more, would predict wildly (a new 5-hour window "running out" in minutes)
    early = dt.timedelta(minutes=15) if span == FIVE_HOURS else dt.timedelta(hours=6)
    if basis < 10 or now - start < early:
        rate = None
    runout = None
    if pct_now >= 100:
        runout = now
    elif rate:
        runout = now + dt.timedelta(hours=(100 - pct_now) / rate)
    return {
        "pct": pct,
        "reset": reset,
        "read_at": read_at,
        "since": since,
        "refilled": since if refill else None,
        "gone": (now - start) / span,
        "allowance": allowance,
        "left": allowance.scaled(max(0.0, 1 - pct_now / 100)) if allowance else None,
        "rate": rate,
        "recent": "30 min" if span == FIVE_HOURS else "24 h",
        "runout": runout if runout and runout < reset else None,
        "at_reset": None if rate is None else pct_now + rate * (reset - now).total_seconds() / 3600,
    }


def pace_text(info, look, caps=False, width=None):
    """One line: when the window runs out at the recent pace, or where it ends up at reset.
    With `width`, padded to it, so it lines up with the meter above when centred."""
    t = _pace_text(info, look, caps)
    if width:
        t.truncate(width, overflow="ellipsis", pad=True)
    return t


def pace_lines(info, look, width, rows):
    """The pace line wrapped onto at most `rows` lines (one at least) of `width`; what still does not fit ends in …"""
    t = _pace_text(info, look, False)
    lines = textwrap.wrap(t.plain, width) or [""]
    rows = max(1, rows)
    if len(lines) > rows:
        lines = lines[:rows - 1] + [" ".join(lines[rows - 1:])]
    return [Text(line, style=t.style, no_wrap=True, overflow="ellipsis") for line in lines]


def _pace_text(info, look, caps):
    up = (lambda x: x.upper()) if caps else (lambda x: x)
    if info["runout"]:
        early = info["reset"] - info["runout"]
        text = f"⚠ at the last {info['recent']}'s pace it runs out ~{info['runout']:%H:%M}, {until(early)} before it resets"
        return Text(up(text), style=f"bold {look.danger if early > dt.timedelta(hours=1) else look.warn}",
                    no_wrap=True, overflow="ellipsis")
    if info["rate"] is None:
        return Text(up("too early in the window to tell the pace"), style=look.muted, no_wrap=True, overflow="ellipsis")
    if info["rate"] <= 0:
        return Text(up(f"nothing used in the last {info['recent']}"), style=look.muted, no_wrap=True, overflow="ellipsis")
    return Text(up(f"at the last {info['recent']}'s pace: ~{min(info['at_reset'], 100):.0f}% used when it resets"),
                style=look.muted, no_wrap=True, overflow="ellipsis")


def active_sessions(recs, now):
    by_session = defaultdict(list)
    for r in recs:
        by_session[r.session].append(r)
    active = [(max(r.ts for r in rs), s, rs) for s, rs in by_session.items()]
    active = [a for a in active if now - a[0] <= dt.timedelta(minutes=ACTIVE_MIN)]
    return sorted(active, key=lambda a: a[0], reverse=True)


def session_info(reader, sid, rs):
    """An active session at a glance: its context now, and what each request costs at that size."""
    main = sorted((r for r in rs if not r.sub), key=lambda r: r.ts)
    recent = (main or sorted(rs, key=lambda r: r.ts))[-5:]
    sent = main[-1].sent if main else 0
    return SimpleNamespace(sid=sid, title=reader.title(sid), model=model_cell(main or rs), frac=context_frac(rs),
                           sum=Sum.of(rs), sent=sent, warn=sent >= CONTEXT_WARN[0],
                           per_req=sum(r.usd or 0 for r in recent) / len(recent) if recent else 0)


def per_req_text(a, look, caps=False):
    t = f"~{money(a.per_req)}/req"
    return Text(t.upper() if caps else t, style=f"bold {look.warn}" if a.warn else look.muted)


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
    scoped = []  # the per-model weekly limits (Fable): each one's reading, and what its model used this week
    for kind, _, model, label in limit_windows()[2:]:
        got = limit_info(recs, now, kind, WEEK, model)
        scoped.append(SimpleNamespace(label=label, model=model, info=got if isinstance(got, dict) else None,
                                      reason=got, week=Sum.of(r for r in model_recs(recs, model) if r.ts >= week0)))
    return SimpleNamespace(sinfo=sinfo, slimit=sinfo if isinstance(sinfo, dict) else None, scoped=scoped,
        now=now, recs=recs, reader=reader, week=week, days=days, info=info,
        today=Sum.of(r for r in recs if r.ts >= day0),
        proj=week.scaled(WEEK / elapsed) if elapsed >= dt.timedelta(hours=1) else None,
        limit=info if isinstance(info, dict) else None,
        active=[session_info(reader, s, rs) for _, s, rs in active_sessions(recs, now)],
    )


def kpis(d):
    left = d.limit["left"] if d.limit else None
    return [
        ("This week", d.week.money(), f"{tok(d.week.total)} tok"),
        ("By Sunday", "~" + d.proj.money() if d.proj else "—", f"~{tok(d.proj.total)} tok" if d.proj else "too early"),
        ("Limit left", left.money() if left else "—",
         f"{tok(left.total)} / {tok(d.limit['allowance'].total)} tok" if left else "no estimate"),
    ]


def scoped_detail(s, d):
    """The line under a per-model limit's meter: what its model used this week (from Monday) and what is left;
    when it resets too, if not with the weekly limit."""
    reset, left = s.info["reset"], s.info["left"]
    parts = [] if d.limit and window_ts(d.limit["reset"]) == window_ts(reset) else [
        f"resets {WEEKDAY[reset.weekday()]} {reset:%H:%M} · in {until(reset - d.now)}"]
    parts.append(f"{s.week.money()} this week")
    if left:
        parts.append(f"~{left.money()} left")
    read_at = s.info["read_at"]
    if d.now - read_at > dt.timedelta(minutes=15):  # asked for every 5 minutes: older means the asks are failing
        parts.append(f"as of {when(read_at, read_at.date() == d.now.date())}")
    return " · ".join(parts)


def limit_block(info, reason, label, look, width, now, window, wrap=lambda x: x, detail=None):
    """Four lines for one limit window: its % used, a meter with ┃ at the share of the window gone, when it resets
    (or `detail`), and the pace."""
    if not info:
        return [wrap(Text(f"{label}: {reason}", style=look.muted))]
    pct, gone, reset, left = info["pct"], info["gone"], info["reset"], info["left"]
    at = f"{WEEKDAY[reset.weekday()]} {reset:%H:%M}" if window == "week" else f"{reset:%H:%M}"
    if detail is None:
        detail = f"resets {at} · in {until(reset - now)}" + (f" · ~{left.money()} left" if left else "")
    return [wrap(grid2(Text(label, style=look.muted), Text(f"{pct:g}% used", style="bold"), width=width)),
            wrap(meter(pct / 100, width, look, gone)),
            wrap(time_caption(detail, mark_x(gone, width), width, gone, window, look)),
            wrap(pace_text(info, look, width=width))]


def amber_limit(label, info, reason, window, look, width, now, detail=None):
    """The same in the Amber theme's words: one meter line, one detail line, the pace."""
    if not info:
        return [Text(f"> {label:<15} {str(reason).upper()}", style=AMBER_DIM, no_wrap=True, overflow="ellipsis")]
    pct, gone, reset, left = info["pct"], info["gone"], info["reset"], info["left"]
    prefix = f"> {label:<15} ["
    mw = max(10, width - 31)
    line = Text(prefix, style=AMBER, no_wrap=True)
    line.append_text(meter(pct / 100, mw, look, gone))
    line.append(f"] {pct:5.1f}%", style=f"bold {AMBER}")
    at = f"{WEEKDAY[reset.weekday()]} {reset:%H:%M}" if window == "week" else f"{reset:%H:%M}"
    if detail is None:
        detail = f"resets {at} · in {until(reset - now)}" + (f" · ~{left.money()} left" if left else "")
    caption = time_caption("  " + detail.upper(), len(prefix) + mark_x(gone, mw), width, gone, window, look, caps=True)
    return [line, caption, Text("  ") + pace_text(info, look, caps=True)]


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
        t = Text(no_wrap=True)  # the muted style on the axis alone, or the chart would be dimmed with it
        t.append(f"{money(ticks[r]):>{label_w - 1}} ┤" if r in ticks else " " * label_w + "│", style=look.muted)
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
    # the space between blocks shrinks before anything is left out: a per-model limit's block takes 7 rows more
    gap = [""] * (2 if height >= 28 + 7 * len(d.scoped) else 1)
    parts = [c(Text("T O D A Y", style=look.muted)), "", c(big(d.today.money(), f"bold {look.accent}")), "",
             c(Text(f"{tok(d.today.total)} tokens   ·   {round(d.today.n)} requests   ·   {len(d.active)} active now",
                    style=look.muted)), *gap]
    parts += limit_block(d.slimit, d.sinfo, "current session · 5-hour limit", look, mw, d.now, "5 h", c)
    parts += [*gap, *limit_block(d.limit, d.info, "weekly limit", look, mw, d.now, "week", c)]
    for s in d.scoped:  # a per-model weekly limit (Fable)
        parts += [*gap, *limit_block(s.info, s.reason, s.label, look, mw, d.now, "week", c,
                                     detail=scoped_detail(s, d) if s.info else None)]
    used = len(parts) + 2  # the figure is three lines tall
    if height - used >= len(gap) + 3:
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
        parts += [*gap, c(stats)]
        used += len(gap) + 3
    room = height - used - len(gap)  # what is left for the sessions
    if d.active and room >= 1:
        g = Table.grid(padding=(0, 3))
        g.width = mw
        g.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        for _ in range(4):
            g.add_column(no_wrap=True, justify="right")
        for a in d.active[:room]:
            g.add_row(Text(("⚠ " if a.warn else "") + a.title, style=look.warn if a.warn else look.muted),
                      meter(a.frac or 0, 12, look), f"{(a.frac or 0):.0%}", per_req_text(a, look), a.sum.money())
        parts += [*gap, c(g)]
    return [Align.center(Group(*parts), vertical="middle", height=height)]


def budget_lines(d, look, st, sw, rows, scoped):
    """The Dashboard's budget panel in `rows` lines of `sw`: the figures (`st`), then each weekly limit, the week's
    and the per-model ones (`scoped`), as its % and a meter; under the week's, when it resets, and under a model's,
    what it used this week and has left; then each one's pace. Short of rows, the week's pace stays longest, then
    a model's line and pace, then the caption under the week's ┃. Rows to spare wrap the paces rather than cut
    them, then part the blocks."""
    muted = lambda s: Text(s, style=look.muted, no_wrap=True, overflow="ellipsis")
    shown = [("weekly limit", d.limit, muted(reset_text(d)), True)] if d.limit else []  # (.., the line under, kept)
    shown += [(s.label, s.info, muted(scoped_detail(s, d)), False) for s in scoped]
    free = rows - 4 - sum(3 if kept else 2 for *_, kept in shown) - (0 if d.limit else 2)
    wanted = [w for i, (*_, kept) in enumerate(shown) for w in ([] if kept else [("under", i)]) + [("pace", i)]]
    take = set(wanted[:max(0, free)] + ([("caption", 0)] if d.limit and free > len(wanted) else []))
    free -= len(take)
    gaps = [0] * len(shown)  # blank lines before each block: first one between blocks, then (after the paces' wraps)
    for i in range(1, len(shown)):  # up to two under the figures
        if free > 0:
            gaps[i], free = 1, free - 1
    paces = {}
    for i, (_, info, _, _) in enumerate(shown):
        if ("pace", i) in take:
            paces[i] = pace_lines(info, look, sw, 1 + max(0, free))
            free -= len(paces[i]) - 1
    if shown:
        gaps[0] = min(2, max(0, free))
    out = [st] if d.limit else [st, "", Text(f"weekly limit: {d.info}", style=look.muted)]
    for i, (label, info, under, kept) in enumerate(shown):
        gone = info["gone"]
        out += [""] * gaps[i] + [grid2(Text(label, style=look.muted), Text(f"{info['pct']:g}%", style="bold")),
                                 meter(info["pct"] / 100, sw, look, gone)]
        if ("caption", i) in take:
            out.append(time_caption("", mark_x(gone, sw), sw, gone, "week", look))
        if kept or ("under", i) in take:
            out.append(under)
        out += paces.get(i, [])
    return out


def overview_dash(d, look, width, height):
    side = max(30, width // 3)  # the right column: a third of the window, so it widens and narrows with it
    sw = side - 2  # inside the right column's panels, less their border and padding
    # the rows: the bottom one needs the current session's 6 and a line for each active session, and 11 at
    # least for the week's bars; the top one, the activity chart and the budget, 11. Rows neither needs go
    # first to the budget, until all its lines show, then half each, so both follow the window's height.
    cur_h = 6
    need = max(11, cur_h + 2 + max(1, len(d.active)))
    scoped = [s for s in d.scoped if s.info]
    full = 2 + 4 + (5 if d.limit else 2) + 4 * len(scoped)  # the budget panel with every line it has
    spare = max(0, height - 11 - need)
    first = min(spare, max(0, full - 11))
    top = 11 + first + (spare - first) // 2
    gh = top - 5
    gw = max(18, width - side - 6)
    lines, bucket, peak = activity_chart(d.recs, d.now, gw, gh + 3, look, dark=d.dark)
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
    budget = budget_lines(d, look, st, sw, top - 2, scoped)

    bottom = max(6, height - top)
    ses = Table.grid(expand=True, padding=(0, 1))
    ses.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
    ses.add_column(no_wrap=True, style=look.muted)
    ses.add_column(no_wrap=True)
    for _ in range(3):
        ses.add_column(no_wrap=True, justify="right")
    ses_h = max(3, bottom - cur_h)
    for a in d.active[:ses_h - 2]:
        ses.add_row(Text(("⚠ " if a.warn else "") + a.title, style=look.warn if a.warn else None), a.model,
                    meter(a.frac or 0, 10, look), f"{(a.frac or 0):.0%}", per_req_text(a, look), a.sum.money())
    if not d.active:
        ses.add_row(Text("none", style=look.muted), "", "", "", "", "")
    inner = width - side - 6
    left_bottom = Group(panel(Group(*limit_block(d.slimit, d.sinfo, "5-hour limit", look, inner, d.now, "5 h")),
                              "current session", cur_h),
                        panel(ses, f"sessions · last {ACTIVE_MIN} min", ses_h))

    top_usd = max(s.usd for _, s, _, _ in d.days) or 1
    colw = max(3, sw // 7 - 1)  # the seven bars, a space after each, fill the column's width
    bars = vbars([0 if fut else s.usd / top_usd for _, s, fut, _ in d.days], max(2, bottom - 5), colw, look.accent)
    labels = Text(no_wrap=True)
    for day, _, _, today in d.days:
        labels.append(f"{WEEKDAY[day.weekday()]:^{colw}} ", style="bold" if today else look.muted)
    today_usd = Text(f"today {d.today.money()}  ·  week {d.week.money()}", style=look.muted)

    g = Table.grid(expand=True)
    g.add_column(ratio=1)
    g.add_column(width=side + 2)
    g.add_row(panel(graph, graph_title, gh + 5), panel(Group(*budget), "budget", gh + 5))
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
    for s in d.scoped:  # a per-model weekly limit (Fable)
        parts += ["", *amber_limit(s.label.upper(), s.info, s.reason, "week", look, width, d.now,
                                   detail=scoped_detail(s, d) if s.info else None)]
    parts += ["", Text("> ACTIVE PROCESSES", style=AMBER)]
    room = max(1, height - 22 - sum(4 if s.info else 2 for s in d.scoped))
    for n, a in enumerate(d.active[:room], 1):
        name = Text(("⚠ " if a.warn else "") + a.title, style=look.warn if a.warn else AMBER)
        name.truncate(max(10, width - 66), overflow="ellipsis", pad=True)
        t = Text(f"  {n:02d} ", style=AMBER_DIM, no_wrap=True)
        t.append_text(name)
        t.append(f" {a.model.upper():<10} CTX ", style=AMBER_DIM)
        t.append_text(meter(a.frac or 0, 10, look))
        t.append(f" {(a.frac or 0):4.0%}  ", style=hot)
        req = per_req_text(a, look, caps=True)
        req.pad_left(12 - len(req.plain))
        t.append_text(req)
        t.append(f"  {a.sum.money():>8}", style=hot)
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
        self.detail = None  # (project, session) open on its own page, from a project list
        self.usage_why = None  # why the last ask for the plan's usage failed, with --usage-api
        self.sort = load_state().get("sort") if load_state().get("sort") in SORTS else "cost"  # the project lists' order

    @property
    def dark(self):
        bg = self.background
        return bg is not None and 0.2126 * bg[0] + 0.7152 * bg[1] + 0.0722 * bg[2] < 128  # unknown: light

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
            self.detail = None
            return
        if k == "theme":
            names = list(THEMES)
            self.theme = names[(names.index(self.theme) + 1) % len(names)]
            save_theme(self.theme)
            return
        if self.detail is not None:
            if k in ("enter", "back"):
                self.detail = None
            return
        if k == "sort" and self.page in (1, 2, 3):
            self.sort = SORTS[(SORTS.index(self.sort) + 1) % len(SORTS)]
            save_state(sort=self.sort)
            return
        rows = self.rows.get(self.page)
        if not rows or self.page in (0, SPEND, HISTORY):
            return
        i = self.index()
        if k == "enter":
            kind, project, session = rows[i]
            if kind == "p":
                self.open ^= {project}
            else:
                self.detail = (project, session)
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
    last = {(p, s): max(r.ts for r in rs) for p, ss in tree.items() for s, rs in ss.items()}
    if ui.sort == "recent":  # newest first, projects by their newest session
        p_key = lambda p: max(last[p, s] for s in tree[p])
        s_key = lambda p, s: last[p, s]
    else:  # most spent first
        p_key = lambda p: psum[p].usd
        s_key = lambda p, s: sum(r.usd or 0 for r in tree[p][s])
    rows = []
    for p in sorted(tree, key=p_key, reverse=True):
        rows.append(("p", p, None))
        if p in ui.open:
            for s in sorted(tree[p], key=lambda s: s_key(p, s), reverse=True):
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
    order = "by cost" if ui.sort == "cost" else "by last used"
    summary.append(f"   {len(tree)} project{'s' if len(tree) != 1 else ''} · {total.money()} · {tok(total.total)} tok"
                   f" · {order.upper() if look.caps else order} ", style=look.muted)
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


# ---------------------------------------------------------------- alerts


def notify(title, body, urgent=False):
    """A desktop notification: notify-send on Linux, osascript on macOS; nothing where neither is there."""
    try:
        if shutil.which("notify-send"):
            cmd = ["notify-send", "-a", "cc-usage", "-u", "critical" if urgent else "normal", title, body]
        elif shutil.which("osascript"):
            cmd = ["osascript", "-e", f"display notification {json.dumps(body)} with title {json.dumps(title)}"]
        else:
            return
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


class Alerts:
    """Each alert once per limit window, and again after a refill: past 80%, past 90%, and on course to run out
    before it resets. What was sent is kept in state.json, so a restart does not send it again."""

    def __init__(self, enabled=True, send=notify):
        self.enabled = enabled
        self.send = send
        self.sent = set(load_state().get("alerts", []))

    def check(self, reader, now):
        if not self.enabled:
            return
        recs = list(reader.recs.values())
        fired = []
        for kind, span, model, label in limit_windows():
            info = limit_info(recs, now, kind, span, model)
            if not isinstance(info, dict):
                continue
            reset = info["reset"]
            base = f"{kind}@{window_ts(reset)}" + (f"~{int(info['refilled'].timestamp())}" if info["refilled"] else "")
            at = f"{WEEKDAY[reset.weekday()]} {reset:%H:%M}" if span == WEEK else f"{reset:%H:%M}"
            reached = [th for th in (80, 90) if info["pct"] >= th and f"{base}:{th}" not in self.sent]
            if reached:  # past both at once: one notice, the higher one
                fired.append(([f"{base}:{th}" for th in reached], f"{label}: {info['pct']:g}% used",
                              f"Resets {at} (in {until(reset - now)}).", max(reached) >= 90))
            if info["runout"] and f"{base}:runout" not in self.sent:
                early = until(reset - info["runout"])
                fired.append(([f"{base}:runout"], f"{label} may run out at {info['runout']:%H:%M}",
                              f"{early} before it resets, at the last {info['recent']}'s pace.", True))
        for _, sid, rs in active_sessions(recs, now):  # a session whose context has grown large
            a = session_info(reader, sid, rs)
            reached = [th for th in CONTEXT_WARN if a.sent >= th and f"ctx@{sid}:{th}" not in self.sent]
            if reached:
                fired.append(([f"ctx@{sid}:{th}" for th in reached], f"Context {tok(a.sent)} tokens: {a.title}",
                              f"Each request now costs ~{money(a.per_req)}. /compact or a new session would cut that.",
                              max(reached) >= CONTEXT_WARN[1]))
        for keys, title, body, urgent in fired:
            self.sent.update(keys)
            self.send(title, body, urgent)
        if fired:  # keep a limit window's alerts only until it resets
            self.sent = {k for k in self.sent if k.startswith("ctx@")
                         or int(k.split("@")[1].split(":")[0].split("~")[0]) > now.timestamp()}
            save_state(alerts=sorted(self.sent))


# ---------------------------------------------------------------- one session in detail


def rebuilds(recs):
    """The main-thread requests, after a session's first, that wrote most of their prompt to the cache
    again: the cache had expired, usually after a pause. Returns their ids."""
    by_session = defaultdict(list)
    for r in recs:
        if not r.sub:
            by_session[r.session].append(r)
    out = set()
    for rs in by_session.values():
        rs.sort(key=lambda r: r.ts)
        for r in rs[1:]:
            if r.sent > 20_000 and r.cw5 + r.cw1h > 0.5 * r.sent:
                out.add(id(r))
    return out


def series_chart(values, plot_w, rows, look, fmt, dark):
    """A braille area chart of `values` (two per cell) with a y axis whose labels `fmt` writes."""
    top = nice_ceiling(max(values, default=0))
    blues = BLUES[::-1] if dark else BLUES
    ramp = [blues[min(i, len(blues) - 1)] for i in range(rows)] if look.accent == ACCENT else [look.accent] * rows
    ticks = {0: top, rows // 2: top * (1 - (rows // 2) / rows)}
    lines = []
    for r, row in enumerate(braille([v / top for v in values], plot_w, rows, ramp)):
        t = Text(no_wrap=True)
        t.append(f"{fmt(ticks[r]):>6} ┤" if r in ticks else " " * 7 + "│", style=look.muted)
        t.append_text(row)
        lines.append(t)
    return lines


def session_page(reader, ui, look, now, width, height):
    project, sid = ui.detail
    rs = sorted((r for r in reader.recs.values() if r.session == sid), key=lambda r: r.ts)
    up = (lambda x: x.upper()) if look.caps else (lambda x: x)
    dash = ui.theme == "dash"
    inner = width - (4 if dash else 2)
    s, sub = Sum.of(rs), Sum.of(r for r in rs if r.sub)
    first, last = rs[0].ts, rs[-1].ts
    models = Counter(short_model(r.model) for r in rs).most_common()
    head = Text(" " + reader.title(sid), style="bold", no_wrap=True, overflow="ellipsis")
    head.append(f"   {project} · {sid[:8]}", style=look.muted)
    who = grid2(head, Text(" · ".join(f"{m} ×{n}" for m, n in models[:3]) + " ", style=look.muted))
    line = f" {when(first)} → {when(last)} ({until(last - first)}) · {round(s.n)} requests"
    line += f" ({round(sub.n)} by subagents, {sub.money()})" if sub.n else ""
    line += f" · {tok(s.total)} tok · {s.money()}"
    paid = defaultdict(float)
    for r in rs:
        for k, v in (r.parts or {}).items():
            paid[k] += v
    paid_line = Text(" " + up("paid for: "), style=look.muted, no_wrap=True, overflow="ellipsis")
    paid_line.append(up(" · ".join(f"{k} {money(paid[k])}" for k in PAID_FOR if paid[k] > 0)))
    info = session_info(reader, sid, rs)
    warn = [Text(up(f" ⚠ context {tok(info.sent)} tokens: each request now costs ~{money(info.per_req)} (average of "
                    f"the last 5). /compact or a new session would cut that."), style=f"bold {look.warn}",
                 no_wrap=True, overflow="ellipsis")] if info.warn else []

    plot_w = max(10, inner - 8)
    n, slots = len(rs), plot_w * 2
    cost_vals, ctx_vals, ctx = [], [], 0
    for b in range(slots):  # each slot shows the costliest request in it, and the context of its last main request
        lo = b * n // slots
        seg = rs[lo:max(lo + 1, (b + 1) * n // slots)]
        cost_vals.append(max(r.usd or 0 for r in seg))
        mains = [r.sent for r in seg if not r.sub]
        ctx = mains[-1] if mains else ctx
        ctx_vals.append(ctx)
    chart_rows = 4 if height >= 26 else 3
    charts = [Text(up(" cost of each request ($)"), style=look.muted),
              *series_chart(cost_vals, plot_w, chart_rows, look, money, ui.dark),
              Text(up(" context sent with each request (tokens)"), style=look.muted),
              *series_chart(ctx_vals, plot_w, chart_rows, look, tok, ui.dark)]
    axis = list("─" * plot_w)
    labels = [" "] * (plot_w + 12)
    multi_day = first.date() != last.date()
    free = 0
    for k in range(5):
        x = round(k * (plot_w - 1) / 4)
        axis[x] = "┴"
        stamp = rs[min(n - 1, x * 2 * n // slots)].ts
        label = when(stamp) if multi_day else f"{stamp:%H:%M}"
        left = min(max(0, x - len(label) // 2), plot_w - len(label))
        if left >= free:
            labels[left:left + len(label)] = label
            free = left + len(label) + 1
    charts += [Text(" " * 7 + "└" + "".join(axis), style=look.muted, no_wrap=True),
               Text(" " * 8 + "".join(labels).rstrip(), style=look.muted, no_wrap=True)]

    rebuilt = rebuilds(rs)
    rebuilt_usd = sum((r.parts or {}).get("cache write", 0) for r in rs if id(r) in rebuilt)
    # rows left for the table: who, when, paid for, a gap, the charts, a gap, its title, its head and rule,
    # the rebuild line; the Dashboard drops the who line into its border and adds the border's two
    k = max(1, height - 9 - len(charts) - len(warn) - (1 if dash else 0))
    t = Table(box=box.SIMPLE_HEAD, show_edge=False, expand=True, pad_edge=True, border_style=look.muted,
              header_style=look.muted if ui.theme == "hero" else f"bold {look.accent}")
    for name, opts in [("When", {}), ("Model", {}), ("Sent", {"justify": "right"}), ("Cache wr", {"justify": "right"}),
                       ("Output", {"justify": "right"}), ("Est. $", {"justify": "right"}),
                       ("", {"ratio": 1, "overflow": "ellipsis"})]:
        t.add_column(up(name), no_wrap=True, **opts)
    for r in sorted(rs, key=lambda r: r.usd or 0, reverse=True)[:k]:
        note = "cache rebuilt" if id(r) in rebuilt else "subagent" if r.sub else ""
        t.add_row(when(r.ts, not multi_day), up(short_model(r.model)), tok(r.sent), tok(r.cw5 + r.cw1h), tok(r.out),
                  money(r.usd or 0), Text(up(note), style=look.warn if id(r) in rebuilt else look.muted))
    rebuild_line = Text(up(f" cache rebuilt {len(rebuilt)} time{'s' if len(rebuilt) != 1 else ''} after a pause: "
                           f"{money(rebuilt_usd)} spent writing the context again" if rebuilt else
                           " no cache rebuilds: the context stayed cached between requests"),
                        style=look.muted, no_wrap=True, overflow="ellipsis")
    body = [who, Text(up(line), style=look.muted, no_wrap=True, overflow="ellipsis"), paid_line, *warn, "", *charts, "",
            Text(up(" most expensive requests"), style="bold"), t, rebuild_line]
    if dash:
        return [Panel(Group(*body[1:]), title=Text(f" {reader.title(sid)} ", style=f"bold {look.accent}"),
                      title_align="left", box=box.ROUNDED, border_style=look.accent, height=height)]
    return body


# ---------------------------------------------------------------- where the money goes


def limits_by_project(recs, now, look, up, hero, width):
    """How many points of each limit window each project used: its $ in the window (since the window's last refill;
    for a per-model limit, that model's $) over the window's allowance."""
    windows = limit_windows()
    shares, totals = [], []
    for kind, span, model, _ in windows:
        info = limit_info(recs, now, kind, span, model)
        share = defaultdict(float)
        if isinstance(info, dict) and info["allowance"] and info["allowance"].usd > 0:
            for r in model_recs(recs, model) if model else recs:
                if r.ts >= info["since"]:
                    share[r.project] += (r.usd or 0) / info["allowance"].usd * 100
            totals.append(f"{sum(share.values()):.0f}%")
        else:
            totals.append("—")
        shares.append(share)
    t = Table(box=box.SIMPLE_HEAD, show_edge=False, show_footer=True, expand=True, pad_edge=True, footer_style="bold",
              border_style=look.muted, header_style=look.muted if hero else f"bold {look.accent}")
    t.add_column(up("Project"), no_wrap=True, ratio=1, overflow="ellipsis", footer=up("Used in all"))
    # each limit takes its name and a bar of 10, and every column 2 of padding; where that leaves the project's
    # name under 24, the names lose " limit" and the bars shrink to 6
    wide = width >= 24 + 22 * len(windows) + 2 * (2 * len(windows) + 1)
    bar_w = 10 if wide else 6
    for (*_, label), total in zip(windows, totals):
        t.add_column(up(label if wide else label.removesuffix(" limit")), no_wrap=True, justify="right", footer=total)
        t.add_column("", no_wrap=True, width=bar_w)
    projects = sorted(set().union(*shares), key=lambda p: [sh[p] for sh in shares[1::-1] + shares[2:]], reverse=True)
    show = projects[:5]
    rest = projects[5:]
    pts = lambda v: "—" if v <= 0 else "<1%" if v < 1 else f"{v:.0f}%"
    for p in show + (["(other projects)"] if rest else []):
        vals = [sum(sh[q] for q in rest) if p == "(other projects)" else sh[p] for sh in shares]
        t.add_row(up(p), *[c for v in vals for c in (pts(v), bar(v / 100, bar_w, look.accent))])
    if not projects:
        t.add_row(Text(up("no limit reading yet, or nothing used in these windows"), style=look.muted),
                  *[""] * (2 * len(windows)))
    return t


def spend_page(reader, ui, look, now, width, height):
    day0, week0 = bounds(now)
    recs = list(reader.recs.values())
    ranges = [("Today", [r for r in recs if r.ts >= day0]), ("This week", [r for r in recs if r.ts >= week0]),
              ("All time", recs)]
    up = (lambda x: x.upper()) if look.caps else (lambda x: x)
    rebuilt = rebuilds(recs)
    dash = ui.theme == "dash"

    def table(first, rows, footer=None):
        t = Table(box=box.SIMPLE_HEAD, show_edge=False, show_footer=footer is not None, expand=True, pad_edge=True,
                  border_style=look.muted, footer_style="bold",
                  header_style=look.muted if ui.theme == "hero" else f"bold {look.accent}")
        t.add_column(up(first), no_wrap=True, ratio=1, overflow="ellipsis", footer=up("Total") if footer else "")
        for i, (name, _) in enumerate(ranges):  # each range: the figure, then its share as a bar in a column of its own
            t.add_column(up(name), no_wrap=True, justify="right", footer=footer[i] if footer else "")
            t.add_column("", no_wrap=True, width=6)
        for row in rows:
            t.add_row(*row)
        return t

    def cell(usd, total):
        return [money(usd), bar(usd / total if total else 0, 6, look.accent)]

    totals = [sum(r.usd or 0 for r in rr) for _, rr in ranges]
    by_model = [defaultdict(float) for _ in ranges]
    by_kind = [defaultdict(float) for _ in ranges]
    for i, (_, rr) in enumerate(ranges):
        for r in rr:
            by_model[i][short_model(r.model)] += r.usd or 0
            for k, v in (r.parts or {}).items():
                by_kind[i][k] += v
    models = sorted(by_model[2], key=lambda m: by_model[2][m], reverse=True)
    t1 = table("Model", [[up(m)] + [c for i in range(3) for c in cell(by_model[i][m], totals[i])] for m in models],
               [money(x) for x in totals])
    kinds = [k for k in PAID_FOR if by_kind[2][k] > 0]
    rows = [[up(k)] + [c for i in range(3) for c in cell(by_kind[i][k], totals[i])] for k in kinds]
    rb = [Text(up("  of which: cache rebuilds"), style=look.muted)]
    for i, (_, rr) in enumerate(ranges):
        mine = [r for r in rr if id(r) in rebuilt]
        usd = sum((r.parts or {}).get("cache write", 0) for r in mine)
        rb += [Text(f"{len(mine)}× {money(usd)}", style=look.muted), bar(usd / totals[i] if totals[i] else 0, 6, look.muted)]
    t2 = table("Paid for", rows + [rb])
    sections = [("Your limits, by project", limits_by_project(recs, now, look, up, ui.theme == "hero",
                                                              width - (4 if dash else 0))), ("By model", t1),
                ("By what was paid for", t2)]
    if dash:
        return [Panel(t, title=Text(f" {name} ", style=f"bold {look.accent}"), title_align="left", box=box.ROUNDED,
                      border_style=look.accent) for name, t in sections]
    out = []
    for name, t in sections:
        out += [Text((" > " if look.caps else " ") + up(name), style="bold"), t, ""]
    return out[:-1]


# ---------------------------------------------------------------- the last 30 days


def history_page(reader, ui, look, now, width, height):
    recs = list(reader.recs.values())
    up = (lambda x: x.upper()) if look.caps else (lambda x: x)
    dash = ui.theme == "dash"
    inner = width - (4 if dash else 2)
    day0, _ = bounds(now)
    days = [(day0 - dt.timedelta(days=i)).date() for i in range(29, -1, -1)]  # oldest first, today last
    sums = {d: Sum() for d in days}
    months = defaultdict(Sum)
    month_days = defaultdict(set)
    for r in recs:
        if r.ts.date() in sums:
            sums[r.ts.date()].add(r)
        months[(r.ts.year, r.ts.month)].add(r)
        month_days[(r.ts.year, r.ts.month)].add(r.ts.date())
    total = Sum()
    for d in days:
        for f in fields(total)[:6]:
            setattr(total, f.name, getattr(total, f.name) + getattr(sums[d], f.name))
    busiest = max(days, key=lambda d: sums[d].usd)
    active = sum(1 for d in days if sums[d].n)
    summary = Text(" " + up("Last 30 days"), style="bold", no_wrap=True, overflow="ellipsis")
    summary.append(up(f"   {total.money()} · ~{money(total.usd / 30)} a day · {active} days used · busiest "
                      f"{WEEKDAY[busiest.weekday()]} {busiest:%m-%d} {sums[busiest].money()}"), style=look.muted)

    plot_w = max(30, inner - 10)
    xs = [round(i * plot_w / 30) for i in range(31)]  # each day's first column; the bars share the width out
    rows = max(3, min(10, height - 16))
    top = nice_ceiling(max(sums[d].usd for d in days))
    ticks = {0: top, rows // 2: top * (1 - (rows // 2) / rows)}
    chart = []
    for r in range(rows):
        t = Text(no_wrap=True)
        t.append(f"{money(ticks[r]):>7} ┤" if r in ticks else " " * 8 + "│", style=look.muted)
        for i, d in enumerate(days):
            w = max(1, xs[i + 1] - xs[i] - 1)
            level = sums[d].usd / top * rows - (rows - 1 - r)
            ch = "█" if level >= 1 else VERT[int(level * 8)] if level > 0 else " "
            t.append(ch * w, style=look.accent)
            t.append(" ")
        chart.append(t)
    axis = list("─" * plot_w)
    labels = [" "] * (plot_w + 6)
    free = 0
    for i in sorted(range(29, -1, -7)):  # today, and every seventh day back from it
        x = xs[i] + max(0, (xs[i + 1] - xs[i] - 1) // 2)
        axis[x] = "┴"
        label = "today" if i == 29 else f"{days[i]:%m-%d}"
        left = min(max(0, x - len(label) // 2), plot_w - len(label))
        if left >= free:
            labels[left:left + len(label)] = label
            free = left + len(label) + 1
    chart += [Text(f"{'$0':>7} └" + "".join(axis), style=look.muted, no_wrap=True),
              Text(" " * 9 + up("".join(labels).rstrip()), style=look.muted, no_wrap=True)]

    t = Table(box=box.SIMPLE_HEAD, show_edge=False, expand=True, pad_edge=True, border_style=look.muted,
              header_style=look.muted if ui.theme == "hero" else f"bold {look.accent}")
    for name, opts in [("Month", {}), ("Est. $", {"justify": "right"}), ("Tokens", {"justify": "right"}),
                       ("Requests", {"justify": "right"}), ("Days used", {"justify": "right"}),
                       ("$ a day used", {"justify": "right"}), ("", {"ratio": 1})]:
        t.add_column(up(name), no_wrap=True, **opts)
    keys = sorted(months, reverse=True)[:max(1, height - 8 - len(chart))]
    top_month = max((months[k].usd for k in keys), default=0) or 1
    for k in keys:
        m, n = months[k], len(month_days[k])
        label = dt.date(k[0], k[1], 1).strftime("%Y-%m") + (up(" (so far)") if k == (now.year, now.month) else "")
        t.add_row(label, m.money(), tok(m.total), str(round(m.n)), str(n), money(m.usd / n if n else 0),
                  bar(m.usd / top_month, 16, look.accent))
    title = lambda name: Text(f" {name} ", style=f"bold {look.accent}")
    if dash:
        return [Panel(Group(*chart), title=summary, title_align="left", box=box.ROUNDED, border_style=look.accent),
                Panel(t, title=title("By month"), title_align="left", box=box.ROUNDED, border_style=look.accent)]
    return [summary, "", *chart, "", Text((" > " if look.caps else " ") + up("By month"), style="bold"), t]


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
    if ui.detail and not any(r.session == ui.detail[1] for r in reader.recs.values()):
        ui.detail = None
    if ui.page == 0:
        d = gather(reader, now)
        d.dark = ui.dark
        body = OVERVIEWS[ui.theme](d, look, width, height - 2)
        keys = "←/→ page   t theme   ^C quit"
    elif ui.page == SPEND:
        body = spend_page(reader, ui, look, now, width, height - 2)
        keys = "←/→ page   t theme   ^C quit"
    elif ui.page == HISTORY:
        body = history_page(reader, ui, look, now, width, height - 2)
        keys = "←/→ page   t theme   ^C quit"
    elif ui.detail:
        body = session_page(reader, ui, look, now, width, height - 2)
        keys = "esc / ⌫ back   ←/→ page   t theme   ^C quit"
    else:
        body = project_page(reader, ui, look, now, width, height - 2)
        keys = "←/→ page   ↑/↓ move   ⏎ open   s sort   t theme   ^C quit"
    hint = Text(" " + (keys.upper() if look.caps else keys), style=look.muted, no_wrap=True, overflow="ellipsis")
    unpriced = sorted({r.model for r in reader.recs.values() if r.usd is None})
    if unpriced:
        hint.append(f"   * no price: {', '.join(unpriced)}", style=look.muted)
    if ui.usage_why:
        hint.append(f"   ! {ui.usage_why.upper() if look.caps else ui.usage_why}", style=look.warn)
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
    b"\r": "enter", b"\n": "enter", b" ": "enter", b"t": "theme", b"T": "theme", b"s": "sort", b"S": "sort",
    b"\x7f": "back", b"\x08": "back",
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
            if data[i] == 0x1B and i + 1 == len(data):  # Esc on its own
                keys.append("back")
                i += 1
            elif data[i] == 0x1B and data[i + 1:i + 2] in (b"[", b"]"):  # another escape sequence: skip it whole
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
    ap.add_argument("--no-alerts", action="store_true", help="send no desktop notifications about the limits")
    ap.add_argument("--root", type=Path, default=ROOT, help="transcripts folder (default ~/.claude/projects)")
    api = ap.add_mutually_exclusive_group()
    api.add_argument("--usage-api", dest="usage_api", action="store_true", default=None,
                     help="also ask Anthropic for the plan's usage every 5 min, as /usage does, with Claude Code's "
                          "login token: the only way to see per-model limits such as Fable's (remembered)")
    api.add_argument("--no-usage-api", dest="usage_api", action="store_false", help="stop asking (remembered)")
    args = ap.parse_args()

    if args.usage_api is not None:
        save_state(usage_api=args.usage_api)
        if not args.usage_api:
            USAGE.unlink(missing_ok=True)  # its readings would only grow stale
    ask = load_state().get("usage_api") is True
    reader = Reader(args.root)
    reader.poll()
    ui = UI(PAGE_ARGS[args.page], args.theme or load_theme())
    console = Console()
    if args.once:
        try:
            stale = time.time() - USAGE.stat().st_mtime > USAGE_EVERY
        except OSError:
            stale = True
        if ask and stale:
            ui.usage_why = ask_usage()
        note_refills(reader.recs.values(), dt.datetime.now().astimezone())
        console.print(render(reader, ui, dt.datetime.now().astimezone(), *console.size))
        return

    def screen():
        return render(reader, ui, dt.datetime.now().astimezone(), *console.size)

    def closed(signum, frame):  # the terminal closed (SIGHUP) or a kill (SIGTERM): leave the way Ctrl-C does
        raise KeyboardInterrupt

    signal.signal(signal.SIGHUP, closed)
    signal.signal(signal.SIGTERM, closed)
    # a resize (SIGWINCH) draws the screen again at once rather than at the next read: each signal writes
    # a byte to this pipe, which wakes the select below
    woken, wake = os.pipe()
    os.set_blocking(woken, False)
    os.set_blocking(wake, False)
    signal.set_wakeup_fd(wake)
    signal.signal(signal.SIGWINCH, lambda signum, frame: None)
    try:
        asker = UsageAsker() if ask else None
        if asker:
            asker.start()
        note_refills(reader.recs.values(), dt.datetime.now().astimezone())
        alerts = Alerts(enabled=not args.no_alerts)
        alerts.check(reader, dt.datetime.now().astimezone())
        with raw_keys() as fd:
            ui.background = terminal_background(fd) if fd is not None else None
            ui.live = True
            with Live(screen(), console=console, screen=True, auto_refresh=False) as live:
                next_read = time.monotonic() + args.every
                while True:
                    wait = max(0.0, next_read - time.monotonic())
                    ready = select.select([woken] if fd is None else [fd, woken], [], [], wait)[0]
                    if woken in ready:
                        os.read(woken, 1024)
                    if fd in ready:
                        for k in parse_keys(os.read(fd, 1024)):
                            ui.key(k)
                    if time.monotonic() >= next_read:
                        reader.poll()
                        note_refills(reader.recs.values(), dt.datetime.now().astimezone())
                        alerts.check(reader, dt.datetime.now().astimezone())
                        ui.usage_why = asker.why if asker else None
                        next_read = time.monotonic() + args.every
                    live.update(screen(), refresh=True)
    except KeyboardInterrupt:
        pass
    finally:
        save_theme(ui.theme)  # the theme it was closed on is the one it opens with next time


if __name__ == "__main__":
    main()
