#!/usr/bin/env python3.12
"""EP391 real-time terminal telemetry monitor.

Reads the CSV log produced by reader.py and renders a live terminal
dashboard using rich. No display server, no browser required.

Usage:
    python3.12 monitor.py --log /tmp/telemetry.csv
    python3.12 monitor.py --log /tmp/telemetry.csv --raw
    python3.12 monitor.py --log /tmp/telemetry.csv --window 60
    python3.12 monitor.py --log /tmp/telemetry.csv --raw --tracking-one-led
"""
from __future__ import annotations
import argparse
import math
import time
from collections import deque
from pathlib import Path

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich import box
from rich.text import Text
from rich.columns import Columns

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
from packet import CAL, parse, to_physical, Packet

# ── Config ────────────────────────────────────────────────────────────────────
REFRESH_HZ   = 2          # terminal redraws per second
HISTORY      = 30         # rows kept in the sparkline buffers
SPARK_WIDTH  = 40         # character width of sparklines
BAR_WIDTH    = 30         # width of analog bar gauges

# Spark block chars (low → high)
SPARKS = " ▁▂▃▄▅▆▇█"


# ── CSV tail ──────────────────────────────────────────────────────────────────

class CsvTailer:
    """Non-blocking CSV tail reader.

    Tracks file position purely via readline()/tell()/seek() — never mixes
    with csv.DictReader iteration, which would trigger Python's file
    iterator read-ahead buffering and silently corrupt the position.
    """

    HEADERS = ("t_host", "seq", "vbat", "ibat", "angle", "t1", "t2")

    def __init__(self, path: Path):
        self.path = path
        self.f = path.open("r")
        # Consume header line if present; otherwise rewind.
        first = self.f.readline()
        if not first.startswith("t_host"):
            self.f.seek(0)

    def pull(self) -> list[dict]:
        """Return every complete row available right now. Non-blocking."""
        rows = []
        while True:
            pos = self.f.tell()
            line = self.f.readline()
            if not line or not line.endswith("\n"):
                # EOF or partial write in progress — back up and stop.
                self.f.seek(pos)
                return rows
            parts = line.rstrip("\r\n").split(",")
            if len(parts) == 7:
                rows.append(dict(zip(self.HEADERS, parts)))


# ── Sparkline ─────────────────────────────────────────────────────────────────

def sparkline(vals: list[float], width: int = SPARK_WIDTH,
              lo: float | None = None, hi: float | None = None) -> str:
    if not vals:
        return " " * width
    finite = [v for v in vals if not (isinstance(v, float) and math.isnan(v))]
    if not finite:
        return "?" * width
    lo = lo if lo is not None else min(finite)
    hi = hi if hi is not None else max(finite)
    span = hi - lo if hi != lo else 1.0
    # sample/subsample to width
    step = max(1, len(vals) // width)
    sampled = vals[-width * step :: step][-width:]
    chars = []
    for v in sampled:
        if isinstance(v, float) and math.isnan(v):
            chars.append("?")
        else:
            idx = int((v - lo) / span * (len(SPARKS) - 1))
            idx = max(0, min(len(SPARKS) - 1, idx))
            chars.append(SPARKS[idx])
    return "".join(chars).ljust(width)


def bar(val: float, lo: float, hi: float,
        width: int = BAR_WIDTH, color: str = "green") -> Text:
    """Horizontal filled bar."""
    span = hi - lo if hi != lo else 1.0
    frac = max(0.0, min(1.0, (val - lo) / span))
    filled = round(frac * width)
    t = Text()
    t.append("█" * filled, style=color)
    t.append("░" * (width - filled), style="dim white")
    return t


def compass_rose(angle_deg: int, width: int = 41) -> str:
    """Single-line ASCII compass with a marker at angle_deg."""
    # Map 0-359 to 0..width-1
    pos = round((angle_deg % 360) / 360 * (width - 1))
    line = list("N" + "─" * ((width - 3) // 2) + "S" + "─" * ((width - 3) // 2) + "N")
    # clobber position with marker
    if 0 <= pos < len(line):
        line[pos] = "▲"
    return "".join(line)


def polar_text(angle_deg: int, radius: int = 5) -> list[str]:
    """Tiny ASCII polar plot, returns list of strings (lines)."""
    size = radius * 2 + 1
    grid = [["·"] * (size * 2 + 1) for _ in range(size)]
    cx, cy = radius * 2, radius
    # draw circle outline (approximate)
    for a in range(360):
        r_a = math.radians(a)
        x = cx + round(radius * 2 * math.sin(r_a))
        y = cy - round(radius * math.cos(r_a))
        if 0 <= y < size and 0 <= x < size * 2 + 1:
            grid[y][x] = "○"
    # draw needle
    r_a = math.radians(angle_deg)
    for r in range(1, radius + 1):
        x = cx + round(r * 2 * math.sin(r_a))
        y = cy - round(r * math.cos(r_a))
        if 0 <= y < size and 0 <= x < size * 2 + 1:
            grid[y][x] = "●"
    # center
    grid[cy][cx] = "+"
    # cardinal labels
    grid[0][cx] = "N"
    grid[size - 1][cx] = "S"
    grid[cy][0] = "W"
    grid[cy][size * 2] = "E"
    return ["".join(row) for row in grid]


# ── Dashboard render ──────────────────────────────────────────────────────────

def make_dashboard(bufs: dict, t0: float, seq: int,
                   raw: bool, window: int,
                   tracking_one_led: bool) -> Layout:

    ts = list(bufs["t"])
    elapsed = ts[-1] if ts else 0.0
    n = len(ts)

    # ── helpers ──────────────────────────────────────────────────────────────
    def last(key):
        vals = list(bufs[key])
        if not vals:
            return float("nan")
        return vals[-1]

    def fmt_val(v, fmt, fallback="---"):
        if isinstance(v, float) and math.isnan(v):
            return Text(fallback, style="dim")
        return Text(fmt.format(v), style="bold white")

    def is_nan(x):
        return isinstance(x, float) and math.isnan(x)

    # ── Battery voltage panel ─────────────────────────────────────────────────
    v = last("vbat")
    vbat_spark = sparkline(list(bufs["vbat"]), lo=0)
    vbat_table = Table(box=None, show_header=False, padding=(0, 1))
    vbat_table.add_column(width=14)
    vbat_table.add_column(width=SPARK_WIDTH + 4)
    if is_nan(v):
        vbat_str = "---"
    elif raw:
        vbat_str = f"{v:.0f} cts"
    else:
        vbat_str = f"{v:.2f} V"
    vbat_table.add_row(
        Text(vbat_str, style="bold cyan"),
        Text(vbat_spark, style="cyan") + Text("  history", style="dim"),
    )
    vbat_table.add_row(
        Text(""),
        bar(0 if is_nan(v) else v,
            0, 1023 if raw else 15.0, color="cyan"),
    )
    vbat_panel = Panel(vbat_table, title="[cyan]Battery Voltage[/]",
                       border_style="cyan", padding=(0, 1))

    # ── Current panel ─────────────────────────────────────────────────────────
    i = last("ibat")
    ibat_spark = sparkline(list(bufs["ibat"]), lo=0)
    ibat_table = Table(box=None, show_header=False, padding=(0, 1))
    ibat_table.add_column(width=14)
    ibat_table.add_column(width=SPARK_WIDTH + 4)
    if is_nan(i):
        i_str = "---"
    elif raw:
        i_str = f"{i:.0f} cts"
    else:
        i_str = f"{i*1000:.1f} mA"
    ibat_table.add_row(
        Text(i_str, style="bold green"),
        Text(ibat_spark, style="green") + Text("  history", style="dim"),
    )
    ibat_table.add_row(
        Text(""),
        bar(0 if is_nan(i) else i,
            0, 1023 if raw else 0.5, color="green"),
    )
    ibat_panel = Panel(ibat_table, title="[green]Payload Current[/]",
                       border_style="green", padding=(0, 1))

    # ── Angle panel ───────────────────────────────────────────────────────────
    angle_raw = last("angle")
    have_angle = not is_nan(angle_raw)
    angle = int(angle_raw) if have_angle else 0
    polar_lines = polar_text(angle, radius=5)
    compass = compass_rose(angle, width=41)

    angle_table = Table(box=None, show_header=False, padding=(0, 1))
    angle_table.add_column(width=14)
    angle_table.add_column()
    angle_table.add_row(
        Text(f"{angle}°" if have_angle else "---", style="bold magenta"),
        Text(compass, style="magenta"),
    )
    for pl in polar_lines:
        angle_table.add_row(Text(""), Text(pl, style="magenta"))

    angle_panel = Panel(angle_table, title="[magenta]Sun Angle[/]",
                        border_style="magenta", padding=(0, 1))

    # ── Temperature panels ────────────────────────────────────────────────────
    def temp_panel(key, title, color):
        v = last(key)
        spark = sparkline(list(bufs[key]))
        t = Table(box=None, show_header=False, padding=(0, 1))
        t.add_column(width=14)
        t.add_column(width=SPARK_WIDTH + 4)
        if is_nan(v):
            v_str = "---"
        elif raw:
            v_str = f"{v:.0f} cts"
        else:
            v_str = f"{v:.1f} °C"
        t.add_row(
            Text(v_str, style=f"bold {color}"),
            Text(spark, style=color) + Text("  history", style="dim"),
        )
        hi = 1023 if raw else 80.0
        lo_t = 0 if raw else -10.0
        t.add_row(
            Text(""),
            bar(0 if is_nan(v) else v, lo_t, hi, color=color),
        )
        return Panel(t, title=f"[{color}]{title}[/]",
                     border_style=color, padding=(0, 1))

    if tracking_one_led:
        t1_panel = temp_panel("t1", "LED Sensor", "yellow")
        t2_panel = temp_panel("t2", "Spare / Placeholder", "dark_orange")
    else:
        t1_panel = temp_panel("t1", "Temperature T1", "yellow")
        t2_panel = temp_panel("t2", "Temperature T2", "dark_orange")

    # ── Packet log (last 6 lines) ─────────────────────────────────────────────
    log_table = Table(box=box.SIMPLE_HEAVY, show_header=True,
                      header_style="bold dim white", padding=(0, 1))
    if raw and tracking_one_led:
        col_headers = ("seq", "vbat", "ibat", "angle", "led", "spare")
    elif raw:
        col_headers = ("seq", "vbat", "ibat", "angle", "t1", "t2")
    else:
        col_headers = ("seq", "V_bat", "I_bat", "angle", "t1 °C", "t2 °C")
    for col in col_headers:
        log_table.add_column(col, justify="right", min_width=6)

    def fmt_log(v, key):
        if is_nan(v):
            return "---"
        if raw or key in ("seq", "angle"):
            return str(int(v))
        if key == "vbat":
            return f"{v:.2f}"
        if key == "ibat":
            return f"{v*1000:.0f} mA"
        return f"{v:.1f}"

    recent = list(zip(
        list(bufs["seq"])[-6:],
        list(bufs["vbat"])[-6:],
        list(bufs["ibat"])[-6:],
        list(bufs["angle"])[-6:],
        list(bufs["t1"])[-6:],
        list(bufs["t2"])[-6:],
    ))
    keys = ("seq", "vbat", "ibat", "angle", "t1", "t2")
    for row_vals in recent:
        log_table.add_row(*[fmt_log(v, k) for v, k in zip(row_vals, keys)])
    log_panel = Panel(log_table, title="[dim]Recent Packets[/]",
                      border_style="dim", padding=(0, 1))

    # ── Status bar ────────────────────────────────────────────────────────────
    cal_note = "[dim red]RAW COUNTS[/]" if raw else "[dim green]calibrated[/]"
    if tracking_one_led:
        cal_note += " [dim cyan](one-led tracking mode)[/]"
    status = Text.assemble(
        ("  EP391 Solar Payload  ", "bold white"),
        (f"  samples: {n:4d}  ", "white"),
        (f"elapsed: {elapsed:6.0f} s  ", "white"),
        (f"seq: {seq:5d}  ", "white"),
        (f"{time.strftime('%H:%M:%S')}  ", "white"),
    )
    status.append_text(Text.from_markup(cal_note))
    status_panel = Panel(status, border_style="dim white", padding=(0, 0))

    # ── Layout assembly ───────────────────────────────────────────────────────
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="row1",   size=7),
        Layout(name="row2",   size=7),
        Layout(name="log",    size=11),
        Layout(name="status", size=3),
    )
    layout["header"].update(Panel(
        Text("EP391 Telemetry Ground Station", style="bold white", justify="center"),
        border_style="bright_blue",
    ))
    layout["row1"].split_row(
        Layout(vbat_panel,  name="vbat"),
        Layout(ibat_panel,  name="ibat"),
        Layout(angle_panel, name="angle"),
    )
    layout["row2"].split_row(
        Layout(t1_panel, name="t1"),
        Layout(t2_panel, name="t2"),
        Layout(name="spacer"),   # keeps symmetry with angle panel above
    )
    layout["row2"]["spacer"].update(Panel("", border_style="dim", title="[dim]spare[/]"))
    layout["log"].update(log_panel)
    layout["status"].update(status_panel)

    return layout


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="EP391 terminal telemetry monitor")
    ap.add_argument("--log",    default="telemetry.csv")
    ap.add_argument("--window", type=int, default=HISTORY,
                    help=f"history length in samples (default {HISTORY})")
    ap.add_argument("--raw",    action="store_true",
                    help="show raw ADC counts instead of physical units")
    ap.add_argument("--tracking-one-led", action="store_true",
                    help="interpret T1 as LED raw counts and T2 as spare placeholder")
    args = ap.parse_args()

    path = Path(args.log)
    while not path.exists():
        print(f"waiting for {path} …")
        time.sleep(0.5)

    tailer = CsvTailer(path)

    bufs: dict[str, deque] = {
        k: deque(maxlen=args.window)
        for k in ("t", "seq", "vbat", "ibat", "angle", "t1", "t2")
    }
    t0_ref: list[float | None] = [None]
    last_seq = [0]

    console = Console()

    def render():
        # Drain every row that has arrived since the last frame.
        for row in tailer.pull():
            try:
                t_host = float(row["t_host"])
                raw_pkt = parse(
                    f"{row['seq']},{row['vbat']},{row['ibat']},"
                    f"{row['angle']},{row['t1']},{row['t2']}"
                )
            except (KeyError, ValueError):
                continue
            if raw_pkt is None:
                continue
            if t0_ref[0] is None:
                t0_ref[0] = t_host
            t_rel = t_host - t0_ref[0]
            last_seq[0] = raw_pkt.seq

            if args.raw:
                bufs["t"].append(t_rel)
                bufs["seq"].append(raw_pkt.seq)
                bufs["vbat"].append(raw_pkt.vbat)
                bufs["ibat"].append(raw_pkt.ibat)
                bufs["angle"].append(raw_pkt.angle)
                bufs["t1"].append(raw_pkt.t1)
                bufs["t2"].append(raw_pkt.t2)
            else:
                phys = to_physical(raw_pkt)
                bufs["t"].append(t_rel)
                bufs["seq"].append(phys.seq)
                bufs["vbat"].append(phys.v_bat)
                bufs["ibat"].append(phys.i_bat)
                bufs["angle"].append(phys.angle)
                bufs["t1"].append(phys.t1_c)
                bufs["t2"].append(phys.t2_c)

        return make_dashboard(bufs, t0_ref[0] or 0.0, last_seq[0],
                              args.raw, args.window, args.tracking_one_led)

    with Live(render(), console=console, refresh_per_second=REFRESH_HZ,
              screen=True) as live:
        while True:
            live.update(render())
            time.sleep(1.0 / REFRESH_HZ)


if __name__ == "__main__":
    main()
