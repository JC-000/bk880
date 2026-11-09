import pytest

from bk880.meter import BK880, BK880Error, parse_fetch, parse_frequency, summarize


class FakeSerial:
    """Answers queries the way firmware 1.11 does; setters change state silently."""

    def __init__(self):
        self.state = {"FUNC:IMPA": "C", "FUNC:IMPB": "NULL", "FREQ": "1kHz",
                      "VOLT": "0.6V", "FUNC:EQU": "PAL", "CALC:TOL:STAT": "OFF",
                      "CALC:TOL:RANG": "----", "CALC:TOL:NOM": "-----"}
        self.out = b""
        self.sent = []

    def write(self, data):
        cmd = data.decode().strip()
        self.sent.append(cmd)
        if cmd == "*IDN?":
            self.out += b"B&K Precision,880,123A45678,1.11\r\n"
        elif cmd == "FETC?":
            if self.state["FUNC:IMPA"] == "DCR":
                self.out += b"+1.00000e+03,N\r\n"
            elif self.state["CALC:TOL:STAT"] == "ON":
                self.out += b"+1.00000e-06,-2.50000e+00,N\r\n"
            else:
                self.out += b"+1.00000e-06,+1.20000e-02,N\r\n"
        elif cmd in ("FUNC:IMPB?", "FUNC:EQU?") and self.state["FUNC:IMPA"] == "DCR":
            pass  # the real meter never answers (E10 on the LCD)
        elif cmd.endswith("?"):
            self.out += self.state[cmd[:-1]].encode() + b"\r\n"
        elif " " in cmd:
            key, val = cmd.split(" ", 1)
            val = {"FREQ": {"100": "100Hz", "120": "120Hz", "1000": "1kHz", "10000": "10kHz",
                            "100000": "100kHz"}.get(val, val),
                   "VOLT": {"0.3": "0.3V", "0.6": "0.6V", "1": "1V"}.get(val, val)}.get(key, val)
            self.state[key] = val
            if key in ("FREQ", "VOLT"):
                self.state["FUNC:IMPB"] = "NULL"  # firmware 1.11 quirk
            if key == "CALC:TOL:STAT":
                self.state["FUNC:IMPB"] = "NULL"
                if val == "ON":  # off -> on captures the nominal and clears the range
                    self.state["CALC:TOL:NOM"] = "+1.02564e-06"
                    self.state["CALC:TOL:RANG"] = "----"
            if key == "CALC:TOL:RANG":
                self.state[key] = {"1": "BIN1", "5": "BIN2", "10": "BIN3", "20": "BIN4"}[val]

    def readline(self):
        line, sep, rest = self.out.partition(b"\n")
        self.out = rest
        return line + sep

    def flush(self): pass
    def reset_input_buffer(self): self.out = b""
    def close(self): pass


@pytest.fixture
def meter(monkeypatch):
    fake = FakeSerial()
    monkeypatch.setattr("bk880.meter.serial.Serial", lambda *a, **k: fake)
    monkeypatch.setattr("bk880.meter.time.sleep", lambda s: None)
    m = BK880("/dev/fake", timeout=0.1).open()
    m.fake = fake
    return m


def test_parse_fetch_lcr_and_dcr():
    r = parse_fetch("+2.14377e-14,+0.00000e+00,N\r\n", "C", "D")
    assert (r.primary, r.secondary, r.bin, r.primary_unit) == (2.14377e-14, 0.0, "N", "F")
    r = parse_fetch("+9.18963e+11,N", "DCR", None)
    assert (r.primary, r.secondary, r.primary_unit) == (9.18963e11, None, "ohm")


def test_parse_fetch_over_range():
    assert parse_fetch("-----,-----,N", "L", "Q").primary is None


def test_parse_frequency():
    assert [parse_frequency(s) for s in ("100Hz", "120Hz", "1kHz", "10kHz", "100kHz")] == \
        [100, 120, 1000, 10000, 100000]


def test_config(meter):
    cfg = meter.get_config()
    assert cfg.to_dict() == {"primary": "C", "secondary": None, "frequency_hz": 1000,
                             "level_v": 0.6, "equivalent": "parallel", "tolerance": False,
                             "tolerance_nominal": None, "tolerance_range_pct": None}


def test_dcr_never_sends_rejected_queries(meter):
    meter.set_primary("DCR")
    cfg = meter.get_config()
    assert (cfg.secondary, cfg.frequency_hz, cfg.level_v, cfg.equivalent) == (None,) * 4
    r = meter.fetch()
    assert "FUNC:IMPB?" not in meter.fake.sent and "FUNC:EQU?" not in meter.fake.sent
    assert r.secondary is None and r.primary == 1000.0


def test_setters_verify(meter):
    meter.set_frequency(100000)
    meter.set_level(0.3)
    meter.set_equivalent("series")
    assert (meter.get_frequency(), meter.get_level(), meter.get_equivalent()) == (100000, 0.3, "series")
    with pytest.raises(BK880Error):
        meter.set_frequency(50)


def test_no_reply_raises(meter):
    meter.set_primary("DCR")
    with pytest.raises(BK880Error):
        meter.query("FUNC:IMPB?")


def test_close_returns_to_local(meter):
    meter.close()
    assert meter.fake.sent[-1] == "*GTL"


def test_summarize(meter):
    s = summarize(list(meter.read_many(3)))
    assert s["primary"]["n"] == 3 and s["primary"]["mean"] == pytest.approx(1e-6)
    assert s["primary_unit"] == "F"


def test_frequency_and_level_changes_keep_secondary(meter):
    meter.set_secondary("D")
    meter.set_frequency(120)
    assert meter.get_secondary() == "D"
    meter.set_level(1.0)
    assert meter.get_secondary() == "D"


def test_ac_only_settings_rejected_in_dcr(meter):
    meter.set_primary("DCR")
    sent = len(meter.fake.sent)
    for call in (lambda: meter.set_frequency(1000), lambda: meter.set_level(1.0),
                 lambda: meter.set_equivalent("series"), lambda: meter.set_secondary("D")):
        with pytest.raises(BK880Error, match="DCR mode has no"):
            call()
    assert all(c == "FUNC:IMPA?" for c in meter.fake.sent[sent:])


def test_tolerance_sets_range_after_capturing_nominal(meter):
    meter.set_secondary("D")
    meter.set_tolerance(True, 5)
    cfg = meter.get_config()
    assert (cfg.tolerance, cfg.tolerance_nominal, cfg.tolerance_range_pct) == (True, 1.02564e-06, 5)
    r = meter.fetch()
    assert (r.deviation_pct, r.secondary) == (-2.5, None)
    assert summarize([r])["deviation_pct"]["mean"] == -2.5


def test_tolerance_reenable_recaptures(meter):
    meter.set_tolerance(True, 10)
    meter.fake.sent.clear()
    meter.set_tolerance(True)
    assert meter.fake.sent.index("CALC:TOL:STAT OFF") < meter.fake.sent.index("CALC:TOL:STAT ON")


def test_parse_fetch_tolerance():
    r = parse_fetch("+5.18569e-13,+1.53718e+01,N", "C", None, tolerance=True)
    assert (r.primary, r.secondary, r.deviation_pct) == (5.18569e-13, None, 15.3718)


class FakePortInfo:
    def __init__(self, device, location, vid=0x10C4, serial_number="0001"):
        self.device, self.location, self.vid = device, location, vid
        self.serial_number = serial_number


@pytest.fixture
def two_adapters(monkeypatch):
    """The bench: two CP210x adapters with the same USB serial, plus a CH343."""
    ports = [FakePortInfo("/dev/cu.usbmodem1", "0-1.3", vid=0x1A86, serial_number="5CE5"),
             FakePortInfo("/dev/cu.usbserial-4", "0-1.2"),
             FakePortInfo("/dev/cu.usbserial-0001", "0-1.4")]
    monkeypatch.setattr("bk880.meter.list_ports.comports", lambda: ports)
    opened = []
    monkeypatch.setattr("bk880.meter.serial.Serial", lambda port, *a, **k: opened.append(port))
    for var in ("BK880_PORT", "BK880_SERIAL", "BK880_LOCATION"):
        monkeypatch.delenv(var, raising=False)
    return opened


def test_port_at_location_follows_the_hub_port(two_adapters):
    from bk880.meter import port_at_location
    assert port_at_location("0-1.4") == "/dev/cu.usbserial-0001"
    assert port_at_location("0-1.2") == "/dev/cu.usbserial-4"
    assert two_adapters == []  # nothing was opened


def test_port_at_location_missing(two_adapters):
    from bk880.meter import port_at_location
    with pytest.raises(BK880Error, match=r"0-1\.9.*usbserial-4 \(serial 0001, location 0-1\.2\)"):
        port_at_location("0-1.9")
    with pytest.raises(BK880Error):
        port_at_location("0-1.3")  # not a CP210x


def test_find_port_refuses_to_probe_several_adapters(two_adapters):
    from bk880.meter import find_port
    with pytest.raises(BK880Error, match="BK880_LOCATION"):
        find_port()
    assert two_adapters == []


def test_resolve_port_precedence(two_adapters, monkeypatch):
    from bk880.meter import resolve_port
    monkeypatch.setenv("BK880_LOCATION", "0-1.4")
    assert resolve_port() == "/dev/cu.usbserial-0001"
    monkeypatch.setenv("BK880_PORT", "/dev/cu.other")
    assert resolve_port() == "/dev/cu.other"
    assert resolve_port("/dev/cu.explicit") == "/dev/cu.explicit"
    assert two_adapters == []


@pytest.fixture
def reprogrammed(monkeypatch, two_adapters):
    """The meter's adapter after its USB serial was set to the meter's serial."""
    ports = [FakePortInfo("/dev/cu.usbserial-4", "0-1.2"),
             FakePortInfo("/dev/cu.usbserial-123A45678", "0-1.3", serial_number="123A45678")]
    monkeypatch.setattr("bk880.meter.list_ports.comports", lambda: ports)
    return two_adapters


def test_port_with_serial_ignores_location(reprogrammed):
    from bk880.meter import port_with_serial
    assert port_with_serial("123A45678") == "/dev/cu.usbserial-123A45678"
    with pytest.raises(BK880Error, match="no CP210x serial adapter with USB serial 123"):
        port_with_serial("123")
    assert reprogrammed == []


def test_port_with_serial_refuses_duplicates(two_adapters):
    from bk880.meter import port_with_serial
    with pytest.raises(BK880Error, match="several"):
        port_with_serial("0001")


def test_resolve_port_serial_beats_location(reprogrammed, monkeypatch):
    from bk880.meter import resolve_port
    monkeypatch.setenv("BK880_LOCATION", "0-1.2")
    monkeypatch.setenv("BK880_SERIAL", "123A45678")
    assert resolve_port() == "/dev/cu.usbserial-123A45678"
    assert reprogrammed == []


class StreamSerial:
    """Replays readline() results; b"" is a read timeout."""

    def __init__(self, chunks):
        self.chunks = list(chunks)

    def readline(self):
        return self.chunks.pop(0) if self.chunks else b""

    def reset_input_buffer(self): pass
    def close(self): pass


def run_stream(monkeypatch, chunks, times, n):
    from bk880.meter import Config
    clock = iter(times)
    monkeypatch.setattr("bk880.meter.time.monotonic", lambda: next(clock))
    m = BK880("/dev/fake")
    m._ser = StreamSerial(chunks)
    stalls = []
    config = Config(primary="C", secondary="D", frequency_hz=1000, level_v=1.0,
                    equivalent="series", tolerance=False)
    gen = m.stream(config, on_stall=stalls.append, stall_s=5.0)
    return [next(gen) for _ in range(n)], stalls


def test_stream_warns_once_per_stall(monkeypatch):
    line = b"+1.00000e-06,+1.20000e-02,N\r\n"
    # waiting before the first reading never warns; then a reading, a 6 s gap
    # (one warning, not two), another reading, and a second stall
    chunks = [b"", b"", line, b"", b"", b"", line, b"", b"", line]
    times = [0, 20, 21, 23, 27, 29, 30, 32, 38, 39]
    readings, stalls = run_stream(monkeypatch, chunks, times, 3)
    assert [r.primary for r in readings] == [1e-06] * 3
    assert stalls == [6, 8]


def test_stream_keeps_lines_split_by_timeout(monkeypatch):
    readings, stalls = run_stream(
        monkeypatch, [b"+1.00000e-06,+1.2", b"0000e-02,N\r\n"], [0, 1], 1)
    assert readings[0].secondary == pytest.approx(0.012)
    assert stalls == []


def test_parse_fetch_dcr_tolerance_has_three_fields():
    # Seen on firmware 1.11: DCR with tolerance on sends value, deviation, bin.
    r = parse_fetch("+1.97539e+03,-8.02055e+01,0", "DCR", None, tolerance=True)
    assert (r.primary, r.deviation_pct, r.bin, r.secondary) == (1975.39, -80.2055, "0", None)
