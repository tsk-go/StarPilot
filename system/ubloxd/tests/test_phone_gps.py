import json
import socket
import time

import pytest

from cereal import messaging
import openpilot.system.ubloxd.phone_gps as phone_gps
from openpilot.system.ubloxd.phone_gps import GpsMux, default_gateways, listed_ips


def _cs(body: str) -> str:
  c = 0
  for ch in body:
    c ^= ord(ch)
  return f"${body}*{c:02X}"


RMC_FIX = _cs("GPRMC,013010.00,A,4043.1234,N,07359.8765,W,0.0,87.5,091026,,,A")
RMC_NO_FIX = _cs("GPRMC,013011.00,V,,,,,,,091026,,,N")   # phone in a tunnel: talking, but no fix


class FakePM:
  def __init__(self):
    self.sent = []

  def send(self, service, dat):
    self.sent.append(dat)


@pytest.fixture
def clock(monkeypatch, tmp_path):
  """Controllable monotonic + wall clocks; status, mode and override files in tmp."""
  c = {"mono": 1000.0, "wall": 1_790_000_000.0}
  monkeypatch.setattr(phone_gps.time, "monotonic", lambda: c["mono"])
  monkeypatch.setattr(phone_gps, "_wall", lambda: c["wall"])
  monkeypatch.setattr(phone_gps, "STATUS_PATH", str(tmp_path / "gps_source.json"))
  monkeypatch.setattr(phone_gps, "MODE_PATH", str(tmp_path / "gps_source"))     # absent: default "fused"
  monkeypatch.setattr(phone_gps, "ALLOWED_IPS_PATH", str(tmp_path / "gps_phone_ips"))
  monkeypatch.setattr(phone_gps, "ROUTE_PATH", str(tmp_path / "route"))

  def tick(s):
    c["mono"] += s
    c["wall"] += s
  c["tick"] = tick
  return c


@pytest.fixture
def mux(clock, monkeypatch):
  m = GpsMux(port=0)
  monkeypatch.setattr(m, "_car", lambda: (0.0, None, True))
  yield m
  m.phone.sock.close()


def send_udp(mux, line):
  port = mux.phone.sock.getsockname()[1]
  with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
    s.sendto((line + "\r\n").encode(), ("127.0.0.1", port))
  time.sleep(0.05)


def comma_fix(unix_s, lat=40.7, lon=-73.9):
  dat = messaging.new_message('gpsLocationExternal', valid=True)
  g = dat.gpsLocationExternal
  g.hasFix, g.latitude, g.longitude, g.horizontalAccuracy, g.satelliteCount = True, lat, lon, 5.0, 9
  g.unixTimestampMillis = int(unix_s * 1e3)
  return dat


# ------------------------------------------------------------------ 1. who may send
ROUTE = """Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT
wlan0\t00000000\t012BA8C0\t0003\t0\t0\t600\t00000000\t0\t0\t0
rmnet_data0\t00000000\t0100000A\t0003\t0\t0\t1000\t00000000\t0\t0\t0
wlan0\t002BA8C0\t00000000\t0001\t0\t0\t600\t00FFFFFF\t0\t0\t0
usb0\t00000000\t00000000\t0001\t0\t0\t900\t00000000\t0\t0\t0
"""


def test_default_gateway_is_the_phone_hotspot(tmp_path):
  (tmp_path / "route").write_text(ROUTE)
  # wlan0's gateway (the phone's hotspot) only: not the carrier's, not a plain subnet route, not a route without gateway
  assert default_gateways(str(tmp_path / "route")) == {"192.168.43.1"}


def test_override_list(tmp_path):
  (tmp_path / "ips").write_text("192.168.1.50, 192.168.1.51  # phone on home Wi-Fi\nnot-an-ip\n# 10.0.0.9\n")
  assert listed_ips(str(tmp_path / "ips")) == {"192.168.1.50", "192.168.1.51"}
  assert listed_ips(str(tmp_path / "missing")) == set()


def test_allowed_senders_follow_the_gateway(clock, mux, tmp_path):
  (tmp_path / "route").write_text(ROUTE)
  assert mux.phone.allowed_senders() == {"192.168.43.1", "127.0.0.1"}
  # the phone's hotspot restarted with another range: picked up on the next refresh, no setup
  (tmp_path / "route").write_text(ROUTE.replace("012BA8C0", "010AA8C0"))
  clock["tick"](phone_gps.ALLOWED_REFRESH_S)
  assert "192.168.10.1" in mux.phone.allowed_senders()
  (tmp_path / "gps_phone_ips").write_text("192.168.1.50")
  clock["tick"](phone_gps.ALLOWED_REFRESH_S)
  assert "192.168.1.50" in mux.phone.allowed_senders()


def test_other_senders_are_dropped_and_counted(clock, mux, monkeypatch):
  monkeypatch.setattr(mux.phone, "allowed_senders", lambda: {"192.168.43.1"})   # 127.0.0.1 is now a stranger
  send_udp(mux, RMC_FIX)
  mux.poll(FakePM())
  assert mux.phone.packets == 0 and not mux.phone_active()
  st = json.load(open(phone_gps.STATUS_PATH))
  assert st["phone"]["rejected"] == 1 and st["phone"]["rejectedFrom"] == {"127.0.0.1": 1}


# ------------------------------------------------------------------ 2. without a phone: exactly as before
def test_no_phone_passes_the_comma_fix_through(clock, mux):
  pm = FakePM()
  for _ in range(5):
    assert mux.internal(comma_fix(clock["wall"])) is True     # published unchanged, by ubloxd itself
    mux.poll(pm)
    clock["tick"](1.0)
  assert pm.sent == []                                         # no fused output
  assert mux.source == "internal (no phone)"


def test_phone_takes_over_then_hands_back(clock, mux):
  pm = FakePM()
  mux.internal(comma_fix(clock["wall"]))                       # the filter is warm from the comma's own fix
  send_udp(mux, RMC_NO_FIX)                                    # phone talking without a fix still counts
  mux.poll(pm)
  assert mux.phone_active() and mux.source == "fused"
  assert len(pm.sent) == 1 and pm.sent[0].gpsLocationExternal.hasFix   # fused output from the first phone packet on
  clock["tick"](0.2)
  assert mux.internal(comma_fix(clock["wall"])) is False       # comma's own fix no longer published directly
  mux.poll(pm)
  assert len(pm.sent) == 2
  clock["tick"](phone_gps.PHONE_ACTIVE_S + 1)                 # phone gone for 30 s
  assert mux.internal(comma_fix(clock["wall"])) is True
  n = len(pm.sent)
  mux.poll(pm)
  assert len(pm.sent) == n


# ------------------------------------------------------------------ 3. satellite time in the fused output
def test_fused_output_carries_satellite_time(clock, mux):
  sat = clock["wall"] + 3600.0                                 # the comma's clock is an hour behind
  mux.internal(comma_fix(sat))
  send_udp(mux, RMC_NO_FIX)
  clock["tick"](0.5)
  pm = FakePM()
  mux.poll(pm)
  stamp = pm.sent[-1].gpsLocationExternal.unixTimestampMillis / 1e3
  assert abs(stamp - (sat + 0.5)) < 0.01                       # timed sees a 1 h difference and fixes the clock
  # timed resets the clock: the filter and the offset run on the monotonic clock, so neither notices
  n = len(pm.sent)
  clock["wall"] += 3600.0
  clock["tick"](0.5)
  mux.poll(pm)
  assert len(pm.sent) == n + 1                                 # no pause after the correction
  assert abs(pm.sent[-1].gpsLocationExternal.unixTimestampMillis / 1e3 - (sat + 1.0)) < 0.01
  assert abs(mux.time_offset - (sat - 1000.0)) < 0.01


def test_late_fix_is_placed_at_its_satellite_time(clock, mux):
  sat = clock["wall"] + 3600.0                                 # wrong comma clock: the fix's age comes from the offset
  mux.internal(comma_fix(sat))
  clock["tick"](1.0)
  assert abs(mux.fix_mono(sat + 0.4, "phone") - (clock["mono"] - 0.6)) < 1e-6   # sent 0.6 s ago
  assert mux.fix_mono(0.0, "phone", time_ok=False) == clock["mono"] - 0.4      # unreadable time: usual delay


def test_one_far_off_reading_moves_time_only_a_little(clock, mux):
  mux.internal(comma_fix(clock["wall"]))
  base = mux.sat_time()
  clock["tick"](1.0)
  mux.internal(comma_fix(clock["wall"] + 500.0))              # a bogus time
  assert abs(mux.sat_time() - (base + 1.0)) <= phone_gps.TIME_STEP_MAX_S + 1e-6


def test_comma_time_preferred_over_phone(clock, mux):
  mux.internal(comma_fix(clock["wall"]))
  before = mux.time_offset
  mux._time_reading(clock["wall"] + 1.5, "phone")              # phone time (with its relay delay) right after
  assert mux.time_offset == before and mux.time_src == "comma"
  clock["tick"](10.0)                                          # comma's GPS gone quiet: phone time is used
  mux._time_reading(clock["wall"] + 1.5, "phone")
  assert mux.time_src == "phone"
