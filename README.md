# bk880

Python driver, command-line tool and [MCP](https://modelcontextprotocol.io) server
for the **B&K Precision 880** handheld LCR meter, over its mini-USB virtual COM
port. Use it to script measurements, log readings to CSV, sort parts against a
reference, or let Claude Desktop take and interpret readings directly.

- Reads and changes every setting the remote interface offers (L/C/R/Z/DCR,
  D/Q/θ/ESR, 100 Hz to 100 kHz, 0.3/0.6/1 V, series/parallel, tolerance) and
  checks each change by reading it back, because the meter never acknowledges
  commands.
- Works around firmware quirks found on real hardware (see
  [Protocol notes](#protocol-notes)).
- Takes an exclusive lock on the port, so the CLI and the MCP server can share
  one meter without reading each other's replies.
- Finds the meter by its USB adapter's serial number, so it is never confused
  with another CP210x adapter on the same machine.

Not affiliated with or endorsed by B&K Precision.

## Requirements

- A B&K Precision 880 (tested with firmware 1.11) and a mini-USB cable. The meter
  runs on its own battery or 12 V supply, not USB power, so it has to be switched on.
- Python 3.11+ and [uv](https://docs.astral.sh/uv/) (or pip).
- macOS, where it is tested. The meter enumerates as a Silicon Labs CP2102
  (`/dev/cu.usbserial-*`). The code uses only pyserial, so Linux
  (`/dev/ttyUSB*`) should work, but has not been tried.

## Install

```sh
git clone https://github.com/JC-000/bk880.git
cd bk880
uv sync --all-extras        # or: pip install -e '.[mcp]'
uv run bk880 idn            # B&K Precision,880,<serial>,1.11
```

The extra `mcp` dependency is only needed for the Claude Desktop server.

## Finding the meter

With a single CP210x serial adapter attached, the port is auto-detected by
sending it `*IDN?`. With several, auto-detect refuses rather than probe the
others, and you name the meter's adapter instead. `bk880 ports` lists the
adapters with their USB serial numbers and locations, without opening any:

```sh
bk880 ports            # /dev/cu.usbserial-123A45678  serial 123A45678  location 0-1.4
export BK880_SERIAL=123A45678   # your adapter's serial
```

Stock CP2102s all report USB serial `0001`, and macOS names their ports in the
order it finds them, so with two attached a replug can swap
`/dev/cu.usbserial-4` and `/dev/cu.usbserial-0001`. That rules out `BK880_PORT`
(a device path). The meter's adapter can be reprogrammed to report the meter's
own serial, so `BK880_SERIAL` finds it on any USB port. Without reprogramming,
`BK880_LOCATION` (the hub port its cable is in, such as `0-1.4`) works as long
as the cable stays in that port. Order of precedence: `--port`, `$BK880_PORT`,
`$BK880_SERIAL`, `$BK880_LOCATION`, auto-detect.

| Variable | Selects the meter by | Example |
|---|---|---|
| `BK880_PORT` | device path | `/dev/cu.usbserial-0001` |
| `BK880_SERIAL` | USB serial of its adapter | `123A45678` |
| `BK880_LOCATION` | USB hub port | `0-1.4` |

### Giving the meter's adapter a unique USB serial

The CP2102's serial string sits in its configuration memory and can be rewritten
over USB (Silicon Labs vendor request 0xFF, register 0x3704, the same request
cp210x-program uses). With the meter as the only CP210x attached:

```sh
brew install libusb
uv run --with pyusb python scripts/cp210x_serial.py                    # read: chip, lock, serial
uv run --with pyusb python scripts/cp210x_serial.py --write 123A45678  # then replug
```

The script uses Homebrew's libusb path (`/opt/homebrew/lib`), which is
Apple-silicon macOS; edit it elsewhere. It refuses unless exactly one CP210x is attached and it is an unlocked
CP2102. The vendor and product IDs are not touched, so a bad write still leaves
an adapter that enumerates and can be rewritten.

## CLI

```sh
bk880 ports                                 # CP210x adapters, USB serials, locations (opens none)
bk880 idn                                   # B&K Precision,880,123A45678,1.11
bk880 config                                # current settings as JSON
bk880 set --primary C --freq 120 --level 1  # change settings (verified by read-back)
bk880 fetch -n 10                           # 10 readings as JSON lines + stats on stderr
bk880 log -o run.csv -i 0.5                 # CSV log every 0.5 s until Ctrl-C
bk880 tol on --range 5                      # capture the part in the socket as nominal, ±5 %
bk880 tol off --secondary D                 # leave tolerance mode, re-select D
bk880 record on | bk880 record | bk880 record off   # meter's own max/min/average
bk880 stream -o run.csv                     # passive log of the meter's auto-fetch output
```

`stream` and `record` are CLI-only on purpose: `stream` needs someone at the
meter to press USB, and the meter's own recording statistics are unreliable
(see the protocol notes), so the MCP server does not offer them.

### Sorting parts against a reference

Put the reference part in the socket and run `bk880 tol on --range 5`. The
meter takes that reading as the nominal. Then swap in each part to test:
readings now have `deviation_pct` (percent off nominal) where the secondary
used to be. `bk880 tol off --secondary D` turns it off again.

### Passive logging (auto-fetch)

`bk880 stream` reads the settings, hands the meter back to the front panel and
waits; give the meter's USB button one short press (holding it toggles the
backlight) and it pushes a reading every cycle, about 2.2 per second, which
`stream` writes to CSV without sending anything. You can keep using the front
panel. Changing the function on the panel mid-stream leaves the CSV's parameter
labels stale.

Streaming does not keep the meter awake: it still switches itself off after its
auto power-off delay (5 minutes here). If readings stop for 5 seconds once
they have started, `stream` prints a warning to stderr and keeps waiting, so
turning the meter on and pressing USB again resumes the same log. For longer runs, set AoFF in the meter's utility menu to 15, 30 or 60
minutes or OFF, press a button now and then, or use `bk880 log`, which polls.

Values are SI base units: F, H, ohm (theta in degrees). A blank/`null` primary
value means over range.

## Python

```python
from bk880 import BK880, resolve_port, summarize

with BK880(resolve_port()) as m:   # $BK880_PORT, $BK880_SERIAL, $BK880_LOCATION or auto-detect
    m.set_primary("L"); m.set_secondary("Q"); m.set_frequency(1000)
    print(summarize(list(m.read_many(20))))
```

## Claude Desktop (MCP)

Add this to `~/Library/Application Support/Claude/claude_desktop_config.json`
and restart Claude Desktop (quit with Cmd-Q, then reopen; do the same after
updating the code). Use absolute paths for `uv` (`which uv`) and the checkout,
because Claude Desktop does not inherit your shell's `PATH`. The server looks
the serial up on every call, so it follows a renamed port or a different USB
port without a restart.

```json
{
  "mcpServers": {
    "bk880": {
      "command": "/opt/homebrew/bin/uv",
      "args": ["run", "--directory", "/path/to/bk880", "bk880-mcp"],
      "env": { "BK880_SERIAL": "123A45678" }
    }
  }
}
```

Tools:

| Tool | Does |
|---|---|
| `bk880_identify` | model, serial number, firmware, port |
| `bk880_get_config` | primary/secondary parameter, frequency, level, series/parallel, tolerance |
| `bk880_configure` | change any of the above |
| `bk880_measure` | N readings with mean/stdev/min/max (optionally each reading) |
| `bk880_frequency_sweep` | average readings at each test frequency, then restore the original |
| `bk880_tolerance` | capture the part in the socket as nominal, set the ±range; readings then carry `deviation_pct` |
| `bk880_log_csv` | log N readings to a CSV file and return the stats |

The port is opened per call and the meter is returned to front-panel control
(`*GTL`) afterwards, so the meter's buttons and the CLI still work between calls.

## Protocol notes

From the [880 manual](https://bkpmedia.s3.us-west-1.amazonaws.com/downloads/manuals/en-us/880_manual.pdf)
(pp. 45-54), plus behaviour observed on the hardware:

- 9600 8N1, no flow control; send `\n`, replies end in `\r\n`.
- Set commands and invalid commands **return nothing**. Errors only show on the
  LCD (E10 unknown command, E11 bad parameter, E12 syntax error), so the driver
  reads every setting back to confirm it.
- `FETCh?` returns `primary,secondary,bin` (L/C/R/Z) or `primary,bin` (DCR);
  with tolerance on it is `primary,deviation,bin` in every mode, DCR included.
- `bin` is the meter's last tolerance judgement, not a per-reading result: `1`
  pass, `0` fail, `N` none yet (or tolerance off). The meter only judges when a
  part is inserted while it is in local mode, and beeps once (pass) or three
  times (fail). While the driver polls in remote mode it neither beeps nor
  updates `bin`, so a part that drifts out of range keeps its old `bin`. For
  pass/fail per reading, compare `deviation_pct` with the range.
- With the secondary display off, `FUNC:IMPB?` returns `NULL` and `FETCh?`
  still sends `0.0` for the secondary; the driver reports it as `null`.
- Changing the frequency or level turns the secondary off (`NULL`), after which
  `FETCh?` keeps repeating the last secondary value instead of measuring it.
  The driver re-selects the secondary after those changes.
- `FUNC:IMPB?` and `FUNC:EQU?` get no reply in DCR mode (the LCD shows E10), so
  they are never sent there; config reports frequency, level and equivalent as
  `null` in DCR, and setting them is refused before anything is sent.
- Reopening the port immediately after closing it loses the next command (E10 on
  the LCD), so the driver keeps sessions 0.5 s apart and lets `*GTL` drain before closing.
- macOS lets several programs open the same port, and they then read each
  other's replies. The driver takes an exclusive lock, so the CLI and Claude
  Desktop's server wait for each other (up to 10 s) instead of colliding.
- Tolerance: turning `CALC:TOL:STAT` from OFF to ON captures the current reading
  as nominal and clears the range (so the range must be set after). While it is
  on, the secondary is `NULL` and `FETCh?`'s second field is the % deviation;
  turning it off leaves the secondary `NULL`. Changing the function over serial
  (even to DCR) leaves tolerance on with the old nominal, although the manual
  says a function change turns it off, so run `tol on` again after switching.
- Recording (`CALC:REC`) covers the primary only (the secondary is always 0); the
  first MAX after switching on is left over from the previous run, and
  `PRESent?` always returns `-----`. The `summarize` stats are usually better.
- Any command puts the meter in remote mode (RMT shown, buttons locked except
  USB). The manual calls that button the RMT key; on the meter it is labelled USB.
  Pressing it returns the meter to local until the next command, so during a
  `log` RMT only flashes off briefly. `*LLO` (local lockout), which the manual
  says also locks USB, has no visible effect on firmware 1.11, so the driver
  offers no panel lock.
- The meter runs on its 9 V NiMH battery or a 12 V supply, not USB power; the
  USB cable only powers its CP2102. Left unused for the auto power-off delay
  (AoFF in the utility menu: 5, 15, 30 or 60 minutes or OFF; 5 here) it beeps
  for 10 seconds, then switches itself off; a button press during the beep
  resets the timer. It powers back up on C with no secondary, 1 kHz, 0.6 V,
  parallel, tolerance off, whatever it was set to before, and a powered-off
  meter does not answer over serial. Every serial command restarts the timer: a
  `log` with a reading every 10 s ran for 8 minutes, and the meter switched off
  4 min 55 s after its last command. Auto-fetch output does not restart it: a
  `stream` stopped when the meter switched off 5 minutes after the last command
  (2026-11-09).

## Tests

```sh
uv run pytest
```

Tests use a fake serial port and need no hardware. With a meter attached,
`uv run bk880 config` is a quick end-to-end check.

## License

[MIT](LICENSE)
