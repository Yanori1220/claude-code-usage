#!/usr/bin/env python3
"""Draws the README's screenshots (docs/<theme>.png) from made-up sessions, so no real usage is shown.

    python3 docs/make_screenshot.py              # dash, hero, amber, spend and session
    python3 docs/make_screenshot.py dash         # one of them

Needs Pillow and the DejaVu fonts. Each terminal cell is painted at a fixed place, and the
braille chart dots and the block bars are drawn as shapes, so the picture lines up whatever
fonts the reader has (a text block on GitHub does not: its font gives braille another width).
"""

import datetime as dt
import io
import json
import os
import random
import sys
import tempfile
import uuid
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from rich.cells import cell_len
from rich.console import Console
from rich.style import Style

HERE = Path(__file__).resolve().parent
COLS, ROWS = 100, 24
SCALE = 2  # drawn at twice the size, so it stays sharp on high-density screens
FONTS = Path("/usr/share/fonts/truetype/dejavu")
BG, FG = (0x16, 0x18, 0x1D), (0xD6, 0xDA, 0xE1)  # a dark terminal
SESSIONS = [  # project, title, model, days ago
    ("my-app", "Add OAuth login", "claude-opus-5-5", 0),
    ("my-app", "Fix flaky tests", "claude-fable-5-2", 0),
    ("notes-site", "Dark mode toggle", "claude-opus-5-5", 0),
    ("notes-site", "Initial layout", "claude-sonnet-5-5", 8),
]


def made_up(root, data, now):
    """Transcripts for three sessions over the last few hours, and a limits file."""
    random.seed(7)
    prefix = str(Path.home()).replace("/", "-") + "-"
    for project, title, model, days_ago in SESSIONS:
        sid = str(uuid.UUID(int=random.getrandbits(128)))
        folder = root / (prefix + project)
        folder.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps({"type": "ai-title", "aiTitle": title, "sessionId": sid})]
        end = now - dt.timedelta(days=days_ago)
        t = end - dt.timedelta(hours=random.uniform(4.5, 5.8))
        context = 30_000
        paused = False
        while t < end:
            for _ in range(random.randint(3, 12) if random.random() < 0.25 else random.randint(1, 3)):
                context += random.randint(500, 6000)
                write = context if paused else random.randint(500, 8000)  # after a long pause the cache is cold
                paused = False
                usage = {"input_tokens": 2, "cache_creation_input_tokens": write, "cache_read_input_tokens": context,
                         "output_tokens": random.randint(100, 2500),
                         "cache_creation": {"ephemeral_1h_input_tokens": write, "ephemeral_5m_input_tokens": 0}}
                usage["cache_read_input_tokens"] = context - write
                lines.append(json.dumps({
                    "type": "assistant", "timestamp": t.isoformat(), "requestId": str(uuid.uuid4()),
                    "message": {"id": str(uuid.uuid4()), "model": model, "usage": usage}}))
                t += dt.timedelta(seconds=random.randint(10, 60))
            if random.random() < 0.08:
                t += dt.timedelta(minutes=70)  # a pause longer than the cache lasts
                paused = True
            else:
                t += dt.timedelta(minutes=random.uniform(2, 25))
        (folder / f"{sid}.jsonl").write_text("\n".join(lines) + "\n")
    data.mkdir(parents=True, exist_ok=True)
    (data / "ratelimits.json").write_text(json.dumps({"at": int(now.timestamp() * 1000), "rateLimits": [
        {"kind": "five_hour", "percentUsed": 37, "resetsAt": (now + dt.timedelta(hours=1, minutes=37)).isoformat()},
        {"kind": "seven_day", "percentUsed": 24, "resetsAt": (now + dt.timedelta(days=2, hours=22)).isoformat()},
    ]}))


def rgb(color, default):
    if color is None or color.is_default:
        return default
    t = color.get_truecolor()
    return (t.red, t.green, t.blue)


def blend(a, b, t):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


class Painter:
    def __init__(self):
        size = 15 * SCALE
        self.font = ImageFont.truetype(str(FONTS / "DejaVuSansMono.ttf"), size)
        self.bold = ImageFont.truetype(str(FONTS / "DejaVuSansMono-Bold.ttf"), size)
        self.cw = round(self.font.getlength("M"))
        self.ch = sum(self.font.getmetrics())  # box-drawing strokes span exactly this, so rows join up
        self.pad = 18 * SCALE

    def paint(self, lines, path, edge=BG):
        """`edge` fills the margin: a theme that paints its own background passes that colour."""
        w, h = COLS * self.cw + 2 * self.pad, ROWS * self.ch + 2 * self.pad
        img = Image.new("RGBA", (w, h), edge + (255,))
        d = ImageDraw.Draw(img)
        for y, line in enumerate(lines[:ROWS]):
            x = 0
            for seg in line:
                if seg.control:
                    continue
                st = seg.style or Style()
                fg, bg = rgb(st.color, FG), rgb(st.bgcolor, BG)
                if st.reverse:
                    fg, bg = bg, fg
                if st.dim:
                    fg = blend(fg, bg, 0.45)
                for c in seg.text:
                    n = cell_len(c)
                    X, Y = self.pad + x * self.cw, self.pad + y * self.ch
                    if bg != BG:
                        d.rectangle([X, Y, X + n * self.cw - 1, Y + self.ch - 1], fill=bg)
                    self.glyph(d, c, X, Y, fg, bool(st.bold))
                    x += n
        mask = Image.new("L", (w, h), 0)  # rounded corners, like a window
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius=12 * SCALE, fill=255)
        img.putalpha(mask)
        img.save(path, optimize=True)

    def glyph(self, d, c, X, Y, fg, bold):
        o, cw, ch = ord(c), self.cw, self.ch
        if c == " ":
            return
        if 0x2800 <= o <= 0x28FF:  # braille: eight dots, two columns of four
            r = cw * 0.15
            for i, (cx, cy) in enumerate([(0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (1, 2), (0, 3), (1, 3)]):
                if (o - 0x2800) >> i & 1:
                    px, py = X + cw * (0.28 + 0.44 * cx), Y + ch * (0.14 + 0.24 * cy)
                    d.ellipse([px - r, py - r, px + r, py + r], fill=fg)
            return
        if c == "█":
            d.rectangle([X, Y, X + cw - 1, Y + ch - 1], fill=fg)
            return
        if c in "▁▂▃▄▅▆▇":
            top = Y + ch - ch * ("▁▂▃▄▅▆▇".index(c) + 1) / 8
            d.rectangle([X, top, X + cw - 1, Y + ch - 1], fill=fg)
            return
        if c in "▏▎▍▌▋▊▉":
            d.rectangle([X, Y, X + cw * ("▏▎▍▌▋▊▉".index(c) + 1) / 8 - 1, Y + ch - 1], fill=fg)
            return
        d.text((X, Y), c, font=self.bold if bold else self.font, fill=fg, anchor="la")


def main():
    shots = sys.argv[1:] or ["dash", "hero", "amber", "spend", "session"]
    now = dt.datetime.now(dt.timezone.utc)
    with tempfile.TemporaryDirectory() as tmp:
        root, data = Path(tmp) / "projects", Path(tmp) / "data"
        made_up(root, data, now)
        os.environ["CC_USAGE_DIR"] = str(data)  # read when cc_usage is imported
        sys.path.insert(0, str(HERE.parent))
        import cc_usage as cu

        reader = cu.Reader(root)
        reader.poll()
        painter = Painter()
        for shot in shots:
            theme = shot if shot in cu.THEMES else "dash"
            ui = cu.UI(cu.SPEND if shot == "spend" else 3 if shot == "session" else 0, theme)
            ui.background = BG  # tells the charts they are on a dark background
            if shot == "session":  # the session with the most cache rebuilds, so the page shows one
                recs = list(reader.recs.values())
                rebuilt = cu.rebuilds(recs)
                r = max(recs, key=lambda r: (sum(id(x) in rebuilt for x in recs if x.session == r.session), r.usd))
                ui.detail = (r.project, r.session)
            screen = cu.render(reader, ui, now.astimezone(), COLS, ROWS)
            console = Console(width=COLS, height=ROWS, file=io.StringIO(), color_system="truecolor", force_terminal=True)
            lines = console.render_lines(screen, console.options.update(width=COLS, height=ROWS), pad=True)
            frame = cu.THEMES[theme].frame
            painter.paint(lines, HERE / f"{shot}.png", rgb(Style.parse(frame).bgcolor, BG) if frame else BG)
            print(f"wrote docs/{shot}.png")


if __name__ == "__main__":
    main()
