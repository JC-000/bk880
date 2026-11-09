"""Driver for the B&K Precision 880 handheld LCR meter (USB virtual COM, SCPI).

Protocol notes verified against firmware 1.11:

* 9600 8N1, no flow control. Commands end in ``\\n``; replies end in ``\\r\\n``.
* Set commands return nothing, and neither do bad commands (the meter shows
  E10/E11/E12 on its LCD and beeps). Setters therefore read the value back.
* ``FETCh?`` returns ``primary,secondary,bin`` in L/C/R/Z modes and
  ``primary,bin`` in DCR mode (``primary,deviation,bin`` in any mode with
  tolerance on). Values are in SI base units (F, H, ohm), the secondary is
  D/Q (unitless), THETA (degrees) or ESR (ohm). ``-----`` means over range.
* ``bin`` is the meter's last tolerance judgement: ``1`` pass, ``0`` fail,
  ``N`` none (tolerance off, or no part judged since it was switched on). It
  only updates when the meter judges a newly inserted part, in local mode,
  with one beep (pass) or three (fail); polling in remote mode never updates
  it, so use the deviation for per-reading pass/fail.
* ``FUNCtion:impb?`` and ``FUNCtion:EQUivalent?`` never answer in DCR mode
  (the LCD shows E10), so they are only sent in L/C/R/Z modes. Avoid sending empty lines for the same reason.
* Changing the frequency or level resets the secondary to NULL; with it NULL,
  ``FETCh?`` repeats the last secondary value (or 0.0) rather than a live one.
* Reopening the port immediately after closing it loses the next command, so
  sessions are spaced REOPEN_GAP_S apart and close() lets ``*GTL`` drain.
* Any command puts the meter into remote mode (front panel locked except the
  USB button, which the manual calls the RMT key). ``*GTL`` hands control back to
  the front panel.
* Tolerance: switching ``CALC:TOL:STAT`` from OFF to ON captures the current
  reading as the nominal and clears the range, so the range is set after.
  While it is on, the secondary is NULL and FETCh?'s second field is the
  deviation from nominal in percent. Switching it off leaves the secondary NULL.
  Changing the function over serial (even to DCR) leaves tolerance on with the
  old nominal, unlike the front panel, so switch it on again to recapture.
* Recording (``CALC:REC``) tracks the primary only (secondary reads 0), its
  first MAX after switching on is stale, and ``PRESent?`` always returns -----.
* Auto-fetch (one short press of the USB button from local, two from remote)
  makes the meter send a FETCh?-format line every cycle (about 2.2 per second)
  unprompted. Any command stops it. It does not reset the auto power-off timer.
"""

from __future__ import annotations

import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

import serial
from serial.tools import list_ports

BAUDRATE = 9600
CP210X_VID = 0x10C4

FREQUENCIES = {"100": 100, "120": 120, "1kHz": 1000, "10kHz": 10_000, "100kHz": 100_000}
VOLTAGES = {"0.3V": 0.3, "0.6V": 0.6, "1V": 1.0}
PRIMARY = ("L", "C", "R", "Z", "DCR")
SECONDARY = ("D", "Q", "THETA", "ESR")
EQUIVALENT = {"SER": "series", "PAL": "parallel"}
TOLERANCE_RANGES = (1, 5, 10, 20)
TOLERANCE_BINS = {"BIN1": 1, "BIN2": 5, "BIN3": 10, "BIN4": 20}

# Reopening the port straight after closing it loses the next command (it
# reaches the meter garbled and shows E10), so sessions are kept this far apart.
REOPEN_GAP_S = 0.5
# How long to wait for another bk880 process to release the port.
LOCK_WAIT_S = 10.0
# Auto-fetch sends about 2.2 readings a second; a gap this long after readings
# have started usually means the meter switched itself off (auto power-off).
STREAM_STALL_S = 5.0
_last_close: dict[str, float] = {}

PRIMARY_UNITS = {"L": "H", "C": "F", "R": "ohm", "Z": "ohm", "DCR": "ohm"}
SECONDARY_UNITS = {"D": "", "Q": "", "THETA": "deg", "ESR": "ohm"}


class BK880Error(Exception):
    """Raised when the meter does not answer or rejects a setting."""


def parse_number(text: str) -> float | None:
    """Parse an NR3 field; ``-----`` (over range) and ``----`` become None."""
    text = text.strip()
    if not text or set(text) == {"-"}:
        return None
    return float(text)


def parse_frequency(text: str) -> int:
    text = text.strip()
    if text.endswith("Hz") and text[:-2] in FREQUENCIES:
        return FREQUENCIES[text[:-2]]
    if text in FREQUENCIES:
        return FREQUENCIES[text]
    for label, hz in FREQUENCIES.items():
        if text.lower() == label.lower():
            return hz
    raise BK880Error(f"unrecognised frequency reply {text!r}")


@dataclass
class Config:
    primary: str
    secondary: str | None
    # None in DCR mode, where they do not apply.
    frequency_hz: int | None
    level_v: float | None
    equivalent: str | None
    tolerance: bool
    # Only set while tolerance mode is on.
    tolerance_nominal: float | None = None
    tolerance_range_pct: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Reading:
    primary: float | None
    secondary: float | None
    bin: str
    primary_param: str
    secondary_param: str | None
    # Percent deviation from the tolerance nominal; only in tolerance mode.
    deviation_pct: float | None = None
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    )

    @property
    def primary_unit(self) -> str:
        return PRIMARY_UNITS.get(self.primary_param, "")

    @property
    def secondary_unit(self) -> str:
        return SECONDARY_UNITS.get(self.secondary_param or "", "")

    def to_dict(self) -> dict:
        d = asdict(self)
        d["primary_unit"] = self.primary_unit
        d["secondary_unit"] = self.secondary_unit
        return d


def parse_fetch(
    reply: str, primary_param: str, secondary_param: str | None, tolerance: bool = False
) -> Reading:
    fields = [f.strip() for f in reply.strip().split(",")]
    if len(fields) == 3:
        p, s, b = fields
        if tolerance:
            return Reading(parse_number(p), None, b, primary_param, None, parse_number(s))
        # With the secondary display off (NULL) the meter still sends 0.0.
        secondary = parse_number(s) if secondary_param else None
        return Reading(parse_number(p), secondary, b, primary_param, secondary_param)
    if len(fields) == 2:
        p, b = fields
        return Reading(parse_number(p), None, b, primary_param, None)
    raise BK880Error(f"unexpected FETCh? reply {reply!r}")


def _cp210x_ports() -> list:
    ports = [p for p in list_ports.comports() if p.vid == CP210X_VID]
    # macOS exposes each port as tty.* and cu.*; cu.* does not wait for carrier.
    ports.sort(key=lambda p: "/tty." in p.device)
    return ports


def list_cp210x_ports() -> list[tuple[str, str | None, str | None]]:
    """(device, USB serial, USB location) of each CP210x serial port. Opens none of them."""
    return [(p.device, p.serial_number, p.location) for p in _cp210x_ports()]


def _describe(ports) -> str:
    return ", ".join(f"{p.device} (serial {p.serial_number}, location {p.location})"
                     for p in ports) or "none"


def port_with_serial(usb_serial: str) -> str:
    """Return the CP210x port whose USB serial number is ``usb_serial``, without opening it.

    Stock CP2102s all report ``0001``; the meter's adapter was reprogrammed to
    report the meter's own serial, so this finds it on any port and never
    matches another adapter.
    """
    ports = _cp210x_ports()
    matches = [p.device for p in ports if p.serial_number == usb_serial]
    if not matches:
        raise BK880Error(
            f"no CP210x serial adapter with USB serial {usb_serial}; is the meter connected? "
            f"CP210x ports present: {_describe(ports)}"
        )
    if len({d.replace("/tty.", "/cu.") for d in matches}) > 1:
        raise BK880Error(f"several CP210x adapters report USB serial {usb_serial}: "
                         f"{_describe(ports)}")
    return matches[0]


def port_at_location(location: str) -> str:
    """Return the CP210x port at a USB location such as ``0-1.4``, without opening it.

    The location is the hub path the cable is plugged into, so it survives
    macOS renaming ports when two adapters share a USB serial number.
    """
    ports = _cp210x_ports()
    for p in ports:
        if p.location == location:
            return p.device
    raise BK880Error(
        f"no CP210x serial adapter at USB location {location}; is the meter's cable "
        f"plugged into its usual hub port? CP210x ports present: {_describe(ports)}"
    )


def find_port() -> str:
    """Return the CP210x port that identifies as an 880.

    Only Silicon Labs CP210x ports are probed, and only when exactly one is
    attached, so unrelated serial devices are never sent ``*IDN?``. Set
    BK880_SERIAL, BK880_LOCATION or BK880_PORT when several are attached.
    """
    ports = _cp210x_ports()
    locations = {p.location or p.device for p in ports}
    if len(locations) > 1:
        raise BK880Error(
            f"several CP210x serial adapters are attached: {_describe(ports)}. Set "
            "BK880_SERIAL to the meter adapter's USB serial (or BK880_LOCATION or "
            "BK880_PORT) so the others are not probed."
        )
    for p in ports:
        try:
            with BK880(p.device, timeout=1.0, release_on_close=False) as m:
                if ",880," in m.idn():
                    return p.device
        except (BK880Error, serial.SerialException, OSError):
            continue
    raise BK880Error(
        "no BK 880 found on any CP210x serial port; is it connected and powered on? "
        "Set BK880_SERIAL, BK880_LOCATION or BK880_PORT to skip auto-detection."
    )


def resolve_port(port: str | None = None) -> str:
    """Pick the meter's port: ``port``, then $BK880_PORT, $BK880_SERIAL,
    $BK880_LOCATION, auto-detect."""
    port = port or os.environ.get("BK880_PORT")
    if port:
        return port
    usb_serial = os.environ.get("BK880_SERIAL")
    if usb_serial:
        return port_with_serial(usb_serial)
    location = os.environ.get("BK880_LOCATION")
    if location:
        return port_at_location(location)
    return find_port()


class BK880:
    """Synchronous driver. Use as a context manager or call close()."""

    def __init__(self, port: str, timeout: float = 2.0, release_on_close: bool = True):
        self.port = port
        self.timeout = timeout
        self.release_on_close = release_on_close
        self._ser: serial.Serial | None = None

    # -- connection -------------------------------------------------------

    def open(self) -> "BK880":
        if self._ser is None:
            wait = _last_close.get(self.port, 0.0) + REOPEN_GAP_S - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._ser = self._open_exclusive()
            self._ser.reset_input_buffer()
        return self

    def _open_exclusive(self, wait: float = LOCK_WAIT_S) -> serial.Serial:
        # macOS and Linux let several programs open one port, and they then
        # read each other's replies. exclusive=True takes an flock, so a second
        # bk880 process (CLI, Claude Desktop) waits here instead of colliding.
        deadline = time.monotonic() + wait
        while True:
            try:
                return serial.Serial(self.port, BAUDRATE, timeout=self.timeout, exclusive=True)
            except serial.SerialException as e:
                if "exclusively lock" not in str(e) or time.monotonic() >= deadline:
                    if "exclusively lock" in str(e):
                        raise BK880Error(
                            f"{self.port} is in use by another program (another bk880 CLI "
                            f"run or Claude Desktop's bk880 server); waited {wait:g}s"
                        ) from e
                    raise
                time.sleep(0.2)

    def close(self) -> None:
        if self._ser is not None:
            try:
                if self.release_on_close:
                    self.local()
                    # flush() returns before the USB adapter has sent everything.
                    time.sleep(0.1)
            finally:
                self._ser.close()
                self._ser = None
                _last_close[self.port] = time.monotonic()

    def __enter__(self) -> "BK880":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    # -- transport --------------------------------------------------------

    def write(self, command: str) -> None:
        self.open()
        assert self._ser is not None
        self._ser.write(command.encode("ascii") + b"\n")
        self._ser.flush()

    def query(self, command: str, retries: int = 1) -> str:
        self.open()
        assert self._ser is not None
        for _ in range(retries + 1):
            self._ser.reset_input_buffer()
            self.write(command)
            reply = self._ser.readline()
            if reply.endswith(b"\n"):
                return reply.decode("ascii", errors="replace").strip()
        raise BK880Error(
            f"no reply to {command!r} within {self.timeout}s. The meter does not report "
            "errors over serial: if its LCD shows E10 (unknown command), E11 (bad "
            "parameter) or E12 (syntax error), the command is not valid in the current "
            "mode. The link is still usable; check that the meter is on and connected."
        )

    # -- common commands --------------------------------------------------

    def idn(self) -> str:
        return self.query("*IDN?")

    def local(self) -> None:
        """Return the meter to front-panel (local) control."""
        self.write("*GTL")

    def lockout(self) -> None:
        """Send ``*LLO`` (local lockout), meant to lock every key including USB.

        Firmware 1.11 ignores it: with or without it, remote mode disables every
        key except USB, and USB still hands the meter back to local (2026-11-09).
        Kept for other firmware; nothing in the CLI or MCP server calls it.
        """
        self.write("*LLO")

    # -- configuration ----------------------------------------------------

    def get_primary(self) -> str:
        return self.query("FUNC:IMPA?")

    def get_secondary(self, primary: str | None = None) -> str | None:
        if (primary or self.get_primary()) == "DCR":
            return None
        reply = self.query("FUNC:IMPB?")
        return None if reply == "NULL" else reply

    def get_frequency(self) -> int:
        return parse_frequency(self.query("FREQ?"))

    def get_level(self) -> float:
        return VOLTAGES[self.query("VOLT?")]

    def get_equivalent(self) -> str:
        reply = self.query("FUNC:EQU?")
        return EQUIVALENT.get(reply, reply)

    def get_tolerance(self) -> bool:
        return self.query("CALC:TOL:STAT?") == "ON"

    def get_tolerance_nominal(self) -> float | None:
        return parse_number(self.query("CALC:TOL:NOM?"))

    def get_tolerance_range(self) -> int | None:
        return TOLERANCE_BINS.get(self.query("CALC:TOL:RANG?"))

    def get_config(self) -> Config:
        primary = self.get_primary()
        if primary == "DCR":
            # FUNC:EQU? is rejected (E10) in DCR, and frequency/level do not apply.
            cfg = Config(primary, None, None, None, None, self.get_tolerance())
        else:
            cfg = Config(
                primary=primary,
                secondary=self.get_secondary(primary),
                frequency_hz=self.get_frequency(),
                level_v=self.get_level(),
                equivalent=self.get_equivalent(),
                tolerance=self.get_tolerance(),
            )
        if cfg.tolerance:
            cfg.tolerance_nominal = self.get_tolerance_nominal()
            cfg.tolerance_range_pct = self.get_tolerance_range()
        return cfg

    def _require_ac(self, setting: str) -> None:
        if self.get_primary() == "DCR":
            raise BK880Error(f"DCR mode has no {setting}; set primary to L, C, R or Z first")

    def _set(self, command: str, getter, expected, settle: float = 0.3):
        self.write(command)
        time.sleep(settle)
        actual = getter()
        if actual != expected:
            raise BK880Error(f"{command!r} was not applied: meter reports {actual!r}")

    def set_primary(self, param: str) -> None:
        param = param.upper()
        if param not in PRIMARY:
            raise BK880Error(f"primary must be one of {PRIMARY}, got {param!r}")
        # Mode changes take a moment before the meter reports the new mode.
        self._set(f"FUNC:IMPA {param}", self.get_primary, param, settle=1.0)

    def set_secondary(self, param: str) -> None:
        param = param.upper()
        if param not in SECONDARY:
            raise BK880Error(f"secondary must be one of {SECONDARY}, got {param!r}")
        self._require_ac("secondary parameter")
        self._set(f"FUNC:IMPB {param}", self.get_secondary, param)

    def set_frequency(self, hz: int) -> None:
        if hz not in FREQUENCIES.values():
            raise BK880Error(f"frequency must be one of {sorted(FREQUENCIES.values())} Hz")
        self._require_ac("test frequency")
        self._set_keeping_secondary(f"FREQ {hz}", self.get_frequency, hz)

    def set_level(self, volts: float) -> None:
        if volts not in VOLTAGES.values():
            raise BK880Error(f"level must be one of {sorted(VOLTAGES.values())} V")
        self._require_ac("test level")
        self._set_keeping_secondary(f"VOLT {volts:g}", self.get_level, volts)

    def _set_keeping_secondary(self, command: str, getter, expected) -> None:
        # Firmware 1.11 turns the secondary display off (NULL) on every
        # frequency or level change, after which FETCh? repeats the last
        # secondary value instead of measuring it. Put it back.
        secondary = self.get_secondary()
        self._set(command, getter, expected)
        if secondary is not None:
            self._set(f"FUNC:IMPB {secondary}", self.get_secondary, secondary)

    def set_equivalent(self, mode: str) -> None:
        mode = mode.lower()
        codes = {v: k for k, v in EQUIVALENT.items()}
        if mode not in codes:
            raise BK880Error("equivalent must be 'series' or 'parallel'")
        self._require_ac("series/parallel equivalent circuit")
        self._set(f"FUNC:EQU {codes[mode]}", self.get_equivalent, mode)

    def set_tolerance(self, enabled: bool, range_pct: int | None = None) -> None:
        """Turn tolerance mode on or off.

        Turning it on captures the reading of the part now in the socket as the
        nominal (if it is already on, it is cycled off first to recapture).
        The secondary parameter is NULL afterwards either way; re-select it
        with set_secondary() once tolerance is off.
        """
        if range_pct is not None and range_pct not in TOLERANCE_RANGES:
            raise BK880Error(f"tolerance range must be one of {TOLERANCE_RANGES}")
        if self.get_tolerance():
            self._set("CALC:TOL:STAT OFF", self.get_tolerance, False)
        if not enabled:
            return
        self._set("CALC:TOL:STAT ON", self.get_tolerance, True, settle=1.0)
        if range_pct is not None:
            self._set(f"CALC:TOL:RANG {range_pct}", self.get_tolerance_range, range_pct)

    def get_recording(self) -> bool:
        return self.query("CALC:REC:STAT?") == "ON"

    def set_recording(self, enabled: bool) -> None:
        """Start (clearing previous values) or stop the meter's own max/min/average."""
        self._set(f"CALC:REC:STAT {'ON' if enabled else 'OFF'}", self.get_recording, enabled)

    def get_recorded(self) -> dict:
        """Max, min and average of the primary since recording started (None if off)."""
        return {
            key: parse_number(self.query(f"CALC:REC:{cmd}?").split(",")[0])
            for key, cmd in (("max", "MAX"), ("min", "MIN"), ("average", "AVER"))
        }

    # -- measurement ------------------------------------------------------

    def _params(self) -> tuple[str, str | None, bool]:
        primary = self.get_primary()
        return primary, self.get_secondary(primary), self.get_tolerance()

    def fetch(
        self,
        primary_param: str | None = None,
        secondary_param: str | None = None,
        tolerance: bool | None = None,
    ) -> Reading:
        """Take one reading. Pass the current params to skip re-querying them."""
        if primary_param is None:
            primary_param, secondary_param, tolerance = self._params()
        return parse_fetch(self.query("FETC?"), primary_param, secondary_param, bool(tolerance))

    def read_many(self, count: int, interval: float = 0.0):
        """Yield ``count`` readings, ``interval`` seconds apart (0 = as fast as possible)."""
        params = self._params()
        for i in range(count):
            start = time.monotonic()
            yield self.fetch(*params)
            if interval and i < count - 1:
                time.sleep(max(0.0, interval - (time.monotonic() - start)))

    def stream(self, config: Config, on_stall=None, stall_s: float = STREAM_STALL_S):
        """Yield readings the meter pushes in auto-fetch mode, sending nothing.

        Any command would stop auto-fetch, so the parameter labels come from
        ``config`` (read before the user started auto-fetch) and go stale if
        the function is changed on the front panel.

        Once readings have started, a gap of ``stall_s`` seconds calls
        ``on_stall(seconds_since_last_reading)`` once; it is called again only
        after readings resume and stop again. Streaming does not reset the
        meter's auto power-off timer, so a stall usually means it switched off.
        """
        self.open()
        assert self._ser is not None
        last = None  # monotonic time of the last complete line
        stalled = False
        partial = b""
        while True:
            partial += self._ser.readline()
            now = time.monotonic()
            if not partial.endswith(b"\n"):
                # Timeout, or a line cut by the read timeout: keep the fragment.
                if last is not None and not stalled and now - last >= stall_s:
                    stalled = True
                    if on_stall is not None:
                        on_stall(now - last)
                continue
            line, partial = partial, b""
            last, stalled = now, False
            yield parse_fetch(line.decode("ascii", errors="replace"), config.primary,
                              config.secondary, config.tolerance)


def summarize(readings: list[Reading]) -> dict:
    """Mean, standard deviation, min and max of the primary and secondary values."""
    import statistics

    def stats(values: list[float]) -> dict | None:
        if not values:
            return None
        return {
            "n": len(values),
            "mean": statistics.fmean(values),
            "stdev": statistics.stdev(values) if len(values) > 1 else 0.0,
            "min": min(values),
            "max": max(values),
        }

    first = readings[0] if readings else None
    return {
        "primary_param": first.primary_param if first else None,
        "primary_unit": first.primary_unit if first else None,
        "primary": stats([r.primary for r in readings if r.primary is not None]),
        "secondary_param": first.secondary_param if first else None,
        "secondary_unit": first.secondary_unit if first else None,
        "secondary": stats([r.secondary for r in readings if r.secondary is not None]),
        "deviation_pct": stats([r.deviation_pct for r in readings if r.deviation_pct is not None]),
        "over_range": sum(r.primary is None for r in readings),
    }
