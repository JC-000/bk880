# bk880

Python driver, CLI and Claude Desktop MCP server for the B&K Precision 880 LCR
meter. README.md covers usage and the protocol quirks; read its "Protocol notes"
before changing `src/bk880/meter.py`.

Machine-specific notes (which adapter is the meter, other devices on the bench,
handoff links) belong in `CLAUDE.local.md`, which is not committed.

## Hardware access

- Select the meter explicitly with `BK880_SERIAL` (or `BK880_LOCATION`) when more
  than one CP210x adapter is attached. `uv run bk880 ports` lists adapters
  without opening any; never open another adapter to find out what it is, since
  opening a port toggles DTR/RTS on whatever is behind it. If the meter is
  missing, ask the user rather than probe.
- Go through the driver (it holds an exclusive lock), not raw pyserial, so you
  cannot collide with another bk880 process such as the MCP server.
- Invalid commands make the meter beep and show E10 to whoever is at the bench;
  avoid sending commands the README lists as rejected.
- Leave the meter on C / D, 1 kHz, 1 V, series, tolerance off when done. It
  switches itself off when idle (it beeps first) and powers up on C / no
  secondary, 1 kHz, 0.6 V, parallel; that is not a fault, just set it back. A
  "no reply" error may mean it has switched off: ask the user to turn it on. It
  runs on a 9 V battery or a 12 V supply, never USB power.

## Design decisions

- `stream` and `record` are CLI-only, not MCP tools: `stream` needs someone to
  press USB at the meter and stops at auto power-off; `record`'s statistics are
  unreliable.
- No front-panel lock option: `*LLO` has no effect on firmware 1.11.

## Checks

`uv run pytest` (no hardware). Hardware checks: `uv run bk880 config`, and the
MCP tools can be driven with an `mcp` stdio client against `uv run bk880-mcp`.
