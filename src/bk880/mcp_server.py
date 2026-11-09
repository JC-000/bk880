"""MCP server exposing the BK 880 to Claude Desktop (stdio transport).

The serial port is opened per tool call and the meter is handed back to
front-panel control afterwards, so the CLI and the meter's own buttons keep
working between calls.
"""

from __future__ import annotations

import csv
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

import serial
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .cli import CSV_FIELDS
from .meter import BK880, BK880Error, find_port, resolve_port, summarize

mcp = MCPServer(
    "bk880",
    instructions=(
        "Controls a B&K Precision 880 handheld LCR meter on a lab bench. "
        "Values are SI base units: farads, henries, ohms; theta in degrees. "
        "A null primary value means over range or nothing connected. "
        "A null secondary parameter means the meter's secondary display is off; "
        "turn it on with bk880_configure(secondary='D'|'Q'|'THETA'|'ESR') when "
        "loss, Q, phase or ESR would be useful. No front-panel action is needed. "
        "To sort parts against a reference, have the user put the reference part in "
        "the socket, call bk880_tolerance(enabled=True, range_pct=...), then measure "
        "each part: readings carry deviation_pct from the captured nominal. Judge "
        "pass/fail from deviation_pct against the range; the bin field only updates "
        "when the meter itself judges a part inserted from the front panel. After "
        "changing the primary parameter, call bk880_tolerance again to recapture. "
        "Check bk880_get_config before measuring and tell the user if the "
        "mode does not suit the component. Prefer 120 Hz for capacitors of "
        "10 uF or more and 1 kHz for small capacitors and inductors under 2 mH."
    ),
)

_lock = threading.Lock()
_detected: str | None = None  # auto-detected port, kept so later calls skip probing

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
CHANGES_SETTINGS = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)


def _resolve() -> str:
    # BK880_SERIAL / BK880_LOCATION are looked up on every call: a replug can
    # rename the port, and a cached name could then belong to another device.
    global _detected
    if any(os.environ.get(v) for v in ("BK880_PORT", "BK880_SERIAL", "BK880_LOCATION")):
        return resolve_port()
    if _detected is None:
        _detected = find_port()
    return _detected


@contextmanager
def meter():
    global _detected
    with _lock:
        port = None
        try:
            port = _resolve()
            with BK880(port) as m:
                yield m
        # ToolError is the only exception whose message the SDK passes to the client.
        except BK880Error as e:
            raise ToolError(str(e)) from e
        except (serial.SerialException, OSError) as e:
            _detected = None  # it may have been unplugged or moved
            raise ToolError(f"cannot open {port}: {e}. Check the USB cable and that the "
                            "meter is on; set BK880_SERIAL (see `bk880 ports`) so a "
                            "renamed port is followed.") from e


@mcp.tool(annotations=READ_ONLY)
def bk880_identify() -> dict:
    """Identify the connected meter: manufacturer, model, serial number, firmware."""
    with meter() as m:
        maker, model, serial_no, firmware = (m.idn().split(",") + ["", "", "", ""])[:4]
        return {"manufacturer": maker, "model": model, "serial": serial_no,
                "firmware": firmware, "port": m.port}


@mcp.tool(annotations=READ_ONLY)
def bk880_get_config() -> dict:
    """Current measurement settings: primary (L/C/R/Z/DCR) and secondary (D/Q/THETA/ESR)
    parameters, test frequency, test level, series/parallel equivalent circuit, tolerance mode."""
    with meter() as m:
        return m.get_config().to_dict()


@mcp.tool(annotations=CHANGES_SETTINGS)
def bk880_configure(
    primary: Literal["L", "C", "R", "Z", "DCR"] | None = None,
    secondary: Literal["D", "Q", "THETA", "ESR"] | None = None,
    frequency_hz: Literal[100, 120, 1000, 10000, 100000] | None = None,
    level_v: Literal[0.3, 0.6, 1.0] | None = None,
    equivalent: Literal["series", "parallel"] | None = None,
) -> dict:
    """Change measurement settings; omitted settings are left alone. Changing
    the primary parameter resets the secondary and equivalent mode to the meter's
    defaults for that function, so it is applied first. DCR has no secondary,
    frequency or level. Returns the resulting configuration."""
    with meter() as m:
        if primary:
            m.set_primary(primary)
        if secondary:
            m.set_secondary(secondary)
        if frequency_hz:
            m.set_frequency(frequency_hz)
        if level_v:
            m.set_level(level_v)
        if equivalent:
            m.set_equivalent(equivalent)
        return m.get_config().to_dict()


@mcp.tool(annotations=READ_ONLY)
def bk880_measure(count: int = 5, interval_s: float = 0.0, include_readings: bool = False) -> dict:
    """Take `count` readings (1-200) with the current settings and return
    mean/stdev/min/max for the primary and secondary values. The meter makes about
    3-4 readings per second; interval_s adds spacing. Set include_readings to get
    each individual timestamped reading. Secondary stats are null when the
    secondary parameter is off; enable it with bk880_configure."""
    if not 1 <= count <= 200:
        raise ToolError("count must be between 1 and 200; use bk880_log_csv for longer runs")
    with meter() as m:
        readings = list(m.read_many(count, interval_s))
    result = {"summary": summarize(readings)}
    if include_readings:
        result["readings"] = [r.to_dict() for r in readings]
    return result


@mcp.tool(annotations=CHANGES_SETTINGS)
def bk880_frequency_sweep(
    frequencies_hz: list[int] | None = None, samples_per_point: int = 5
) -> dict:
    """Measure the component at several test frequencies (default: all five,
    100 Hz to 100 kHz), averaging samples_per_point readings at each. The
    original frequency is restored afterwards. Not available in DCR mode."""
    freqs = frequencies_hz or [100, 120, 1000, 10000, 100000]
    with meter() as m:
        cfg = m.get_config()
        if cfg.primary == "DCR":
            raise ToolError("frequency sweep needs L, C, R or Z mode; DCR is a DC measurement")
        points = []
        try:
            for hz in freqs:
                m.set_frequency(hz)
                time.sleep(1.0)  # let the reading settle at the new frequency
                points.append({"frequency_hz": hz,
                               **summarize(list(m.read_many(samples_per_point)))})
        finally:
            m.set_frequency(cfg.frequency_hz)
    return {"config": cfg.to_dict(), "points": points}


@mcp.tool(annotations=CHANGES_SETTINGS)
def bk880_tolerance(
    enabled: bool,
    range_pct: Literal[1, 5, 10, 20] | None = None,
    restore_secondary: Literal["D", "Q", "THETA", "ESR"] | None = None,
) -> dict:
    """Tolerance (part sorting) mode. enabled=True captures the reading of the
    part now in the socket as the nominal (re-capturing if already on) and sets
    the +/- range. While on, the secondary display is replaced by the deviation:
    readings carry deviation_pct instead of a secondary value. enabled=False
    turns it off; the meter leaves the secondary off afterwards, so pass
    restore_secondary to re-select one. Returns the resulting configuration."""
    with meter() as m:
        m.set_tolerance(enabled, range_pct)
        if not enabled and restore_secondary:
            m.set_secondary(restore_secondary)
        return m.get_config().to_dict()


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False))
def bk880_log_csv(path: str, count: int, interval_s: float = 1.0) -> dict:
    """Log `count` readings to a CSV file at `path` (overwritten if present,
    `~` expanded), `interval_s` seconds apart. Blocks until finished, so keep
    count * interval_s to a few minutes. While it runs the meter is in remote
    mode, so every front-panel key except USB is disabled; pressing USB hands it
    back to local only until the next reading. Returns the summary statistics."""
    out = Path(path).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    readings = []
    with meter() as m, out.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for r in m.read_many(count, interval_s):
            writer.writerow(r.to_dict())
            readings.append(r)
    return {"path": str(out.resolve()), "summary": summarize(readings)}


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
