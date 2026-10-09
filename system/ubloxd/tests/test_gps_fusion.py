import math
import random

import openpilot.system.ubloxd.gps_fusion as gps_fusion
from openpilot.system.ubloxd.gps_fusion import GpsFusion, M_PER_DEG
from openpilot.system.ubloxd.phone_gps import PhoneGps, nmea_ok


def _cs(body: str) -> str:
  c = 0
  for ch in body:
    c ^= ord(ch)
  return f"${body}*{c:02X}"


def test_nmea_checksum():
  assert nmea_ok(_cs("GPRMC,013010.00,A,4043.1234,N,07359.8765,W,30.0,87.5,091026,,,A"))
  assert not nmea_ok("$GPRMC,013010.00,A,4043.1234,N,07359.8765,W,30.0,87.5,091026,,,A*00")


def test_phone_rmc_gga_pair():
  p = PhoneGps(port=0)  # any free port, lines fed directly
  p._line(_cs("GNGGA,013010.00,4043.1234,N,07359.8765,W,1,30,0.6,25.3,M,-34.0,M,,"))
  p._line(_cs("GNRMC,013010.00,A,4043.1234,N,07359.8765,W,29.2,87.5,091026,,,A"))
  fixes, p.pending = p.pending, []
  assert len(fixes) == 1
  f = fixes[0]
  assert abs(f["lat"] - (40 + 43.1234 / 60)) < 1e-9
  assert abs(f["lon"] + (73 + 59.8765 / 60)) < 1e-9
  assert f["sats"] == 30
  assert abs(f["speed"] - 29.2 * 0.514444) < 1e-6


def _drive(tmp_path, tunnel=(200, 260), gyro_axis=0, gyro_sign=-1, seconds=400):
  """Simulated drive: S-curves, phone fixes at 1 Hz arriving 0.6 s late, 3 m noise, occasional 60 m multipath jumps,
  a GPS-less tunnel, and the turn rate on an arbitrary gyro axis with an arbitrary sign."""
  gps_fusion.SIGN_PATH = str(tmp_path / "gyro_axis")
  random.seed(1)
  lat0, lon0 = 40.75, -73.99
  f = GpsFusion()
  x = y = hdg = 0.0
  t, dt, v = 1000.0, 0.05, 15.0
  pending, errs = [], {"gps": [], "tunnel": []}
  for _ in range(int(seconds / dt)):
    t += dt
    ph = (t - 1000) % 40
    w = math.radians(9.0) if 10 < ph < 20 else (-math.radians(9.0) if 30 < ph < 40 else 0.0)
    hdg = (hdg + math.degrees(w) * dt) % 360
    x += v * dt * math.sin(math.radians(hdg))
    y += v * dt * math.cos(math.radians(hdg))
    in_tunnel = tunnel[0] < t - 1000 < tunnel[1]
    if round(t, 2) % 1 == 0 and not in_tunnel:
      nx, ny = x + random.gauss(0, 3), y + random.gauss(0, 3)
      if random.random() < 0.03:
        nx += 60
      pending.append((t + 0.6, lat0 + ny / M_PER_DEG, lon0 + nx / (M_PER_DEG * math.cos(math.radians(lat0))),
                      (hdg + random.gauss(0, 2)) % 360, t))
    g = [random.gauss(0, 0.01), random.gauss(0, 0.01), random.gauss(0, 0.01)]
    g[gyro_axis] += gyro_sign * w + 0.002
    f.predict(t, v / 1.02, tuple(g), True)
    while pending and pending[0][0] <= t:
      _, la, lo, crs, ft = pending.pop(0)
      f.gps(t, la, lo, 0.0, 3.0, v, crs, ft, 20, "phone")
    if t - 1000 > 30:
      e = math.hypot((f.lon - lon0) * M_PER_DEG * math.cos(math.radians(lat0)) - x, (f.lat - lat0) * M_PER_DEG - y)
      errs["tunnel" if in_tunnel else "gps"].append(e)
  return f, errs


def test_fusion_tracks_gps_and_learns_gyro(tmp_path):
  f, errs = _drive(tmp_path, gyro_axis=0, gyro_sign=-1)
  assert (f.axis, f.sign) == (0, -1)
  assert sum(errs["gps"]) / len(errs["gps"]) < 4.0
  assert f.rejected > 0                       # multipath jumps were thrown out
  assert abs(f.scale - 1.02) < 0.02           # wheel-speed scale learned from GPS speed


def test_fusion_dead_reckons_through_tunnel(tmp_path):
  _, errs = _drive(tmp_path, gyro_axis=2, gyro_sign=1)
  assert max(errs["tunnel"]) < 60.0           # 900 m with curves and no GPS
