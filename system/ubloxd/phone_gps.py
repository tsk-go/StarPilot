#!/usr/bin/env python3
"""
Phone GPS for the comma (StarView). The comma's own u-blox gets jammed by USB 3 noise from the hub (3 satellites with
the tablet attached), so a phone in the car sends its GPS over Wi-Fi as NMEA (UDP) and ubloxd publishes it as the
normal gpsLocationExternal - navigation, speed-limit maps, the tablet's GPS bridge all just work.

Phone app: gpsdRelay (F-Droid, io.github.project_kaat.gpsdrelay) or GPSd Forwarder - send NMEA over UDP to the
comma's IP (or the network's broadcast address), port 29998.

Who may send: only the comma's default gateway (when the comma is on the phone's hotspot, that IS the phone, so no
setup), the comma itself, and any addresses listed in /data/starview/gps_phone_ips (for a phone and comma on another
Wi-Fi). Anything else could feed a fake position (-> wrong map speed limits), so it's dropped and counted in the status.

Source choice, file /data/starview/gps_source (default "fused"):
  fused    - phone + comma GPS + the car's wheel speed and the comma's gyro, 10 Hz, keeps going in tunnels
             (gps_fusion.py). Only while the phone has sent something in the last PHONE_ACTIVE_S: without a phone the
             comma's own GPS goes out unchanged, exactly as before.
  auto     - the comma's own GPS when it's good (fix, <= 10 m, >= 6 satellites, steadily for 10 s), else the phone
  phone    - the phone whenever it has a fresh fix, else the comma's own
  internal - ignore the phone
Status for checking: /dev/shm/gps_source.json (written once a second).
"""
import calendar
import json
import math
import os
import socket
import time

from cereal import log
from cereal import messaging
from openpilot.system.ubloxd.gps_fusion import GpsFusion


def _wall() -> float:
  return time.time()  # noqa: TID251  wall clock on purpose: compared with GPS UTC times / shared through files


PORT = 29998
MODE_PATH = "/data/starview/gps_source"
STATUS_PATH = "/dev/shm/gps_source.json"
ALLOWED_IPS_PATH = "/data/starview/gps_phone_ips"
ROUTE_PATH = "/proc/net/route"
ALLOWED_REFRESH_S = 5.0
CELLULAR_IFACES = ("rmnet", "wwan", "ccmni")  # the carrier's gateway is not a phone in the car
PHONE_FRESH_S = 2.5
PHONE_ACTIVE_S = 30.0       # fused output only while the phone has sent anything this recently (fix or not: tunnels)
TIME_STEP_MAX_S = 2.0       # one reading moves the satellite-time offset by at most this much
INTERNAL_GOOD_HACC = 10.0
INTERNAL_GOOD_SATS = 6
INTERNAL_GOOD_HOLD_S = 10.0
INTERNAL_BAD_HOLD_S = 2.0
KNOTS = 0.514444


def nmea_ok(line: str) -> bool:
  if not line.startswith("$") or "*" not in line:
    return False
  body, _, cs = line[1:].partition("*")
  try:
    want = int(cs[:2], 16)
  except ValueError:
    return False
  got = 0
  for ch in body:
    got ^= ord(ch)
  return got == want


def _deg(v: str, hemi: str) -> float | None:
  if not v:
    return None
  try:
    dot = v.index(".")
    d = float(v[:dot - 2]) + float(v[dot - 2:]) / 60.0
  except ValueError:
    return None
  return -d if hemi in ("S", "W") else d


def _f(v: str) -> float | None:
  try:
    return float(v) if v else None
  except ValueError:
    return None


def default_gateways(route_path: str | None = None) -> set[str]:
  """IPv4 default gateways from the kernel's routing table, except cellular ones."""
  out = set()
  try:
    with open(route_path or ROUTE_PATH) as f:
      next(f, None)
      for line in f:
        parts = line.split()
        if len(parts) < 4 or parts[1] != "00000000" or parts[0].startswith(CELLULAR_IFACES):
          continue
        if not int(parts[3], 16) & 0x2:   # RTF_GATEWAY
          continue
        gw = socket.inet_ntoa(int(parts[2], 16).to_bytes(4, "little"))
        if gw != "0.0.0.0":
          out.add(gw)
  except (OSError, ValueError):
    pass
  return out


def listed_ips(path: str | None = None) -> set[str]:
  """Extra phone addresses from the override file: separated by spaces, commas or new lines; # starts a comment."""
  out = set()
  try:
    with open(path or ALLOWED_IPS_PATH) as f:
      for line in f:
        for tok in line.split("#", 1)[0].replace(",", " ").split():
          try:
            socket.inet_aton(tok)
            out.add(tok)
          except OSError:
            pass
  except OSError:
    pass
  return out


class PhoneGps:
  def __init__(self, port: int = PORT):
    self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    self.sock.bind(("0.0.0.0", port))
    self.sock.setblocking(False)
    self.buf = ""
    self.sender = ""
    self.packets = 0
    # latest pieces of the current fix
    self.gga: dict = {}
    self.rmc: dict = {}
    self.gst_hacc: float | None = None
    self.bearing = 0.0
    self.last_fix_mono = 0.0
    self.sats = 0
    self.pending: list = []          # finished fixes waiting to be published
    self._last_t = None
    self._gga_mono = 0.0
    self.last_msg_mono = 0.0         # any accepted packet, with or without a fix
    self.allowed: set[str] = set()
    self._allowed_at = float("-inf")
    self.rejected = 0
    self.rejected_from: dict[str, int] = {}

  def allowed_senders(self) -> set[str]:
    now = time.monotonic()
    if now - self._allowed_at >= ALLOWED_REFRESH_S:   # the hotspot's gateway can change after a phone restart
      self._allowed_at = now
      self.allowed = default_gateways() | listed_ips() | {"127.0.0.1"}
    return self.allowed

  def _reject(self, ip: str) -> None:
    self.rejected += 1
    if ip in self.rejected_from or len(self.rejected_from) < 8:
      self.rejected_from[ip] = self.rejected_from.get(ip, 0) + 1

  def poll(self) -> list:
    """Read everything waiting on the socket; returns finished fixes (dicts)."""
    while True:
      try:
        data, addr = self.sock.recvfrom(65535)
      except (BlockingIOError, InterruptedError):
        break
      except OSError:
        break
      if addr[0] not in self.allowed_senders():
        self._reject(addr[0])
        continue
      self.sender = addr[0]
      self.packets += 1
      self.last_msg_mono = time.monotonic()
      self.buf += data.decode("ascii", "ignore")
      if len(self.buf) > 20000:
        self.buf = self.buf[-4000:]
    while "\n" in self.buf:
      line, self.buf = self.buf.split("\n", 1)
      self._line(line.strip())
    if self.buf.startswith("$") and "*" in self.buf and len(self.buf.split("*", 1)[1]) >= 2:
      # UDP senders that put one sentence per packet without a newline
      line, self.buf = self.buf, ""
      self._line(line.strip())
    out, self.pending = self.pending, []
    return out

  def _line(self, line: str) -> None:
    if not nmea_ok(line):
      return
    f = line[1:].split("*")[0].split(",")
    kind = f[0][2:]
    if kind == "GGA" and len(f) >= 10:
      self.gga = {"t": f[1], "lat": _deg(f[2], f[3]), "lon": _deg(f[4], f[5]), "q": int(f[6] or 0),
                  "sats": int(f[7] or 0), "hdop": _f(f[8]), "alt": _f(f[9])}
      self.sats = self.gga["sats"]
      self._gga_mono = time.monotonic()
      self._maybe_emit()
    elif kind == "RMC" and len(f) >= 10:
      self.rmc = {"t": f[1], "ok": f[2] == "A", "lat": _deg(f[3], f[4]), "lon": _deg(f[5], f[6]),
                  "kn": _f(f[7]), "crs": _f(f[8]), "date": f[9]}
      self._maybe_emit()
    elif kind == "GST" and len(f) >= 9:
      a, b = _f(f[6]), _f(f[7])
      if a is not None and b is not None:
        self.gst_hacc = math.hypot(a, b)

  def _maybe_emit(self) -> None:
    r, g = self.rmc, self.gga
    if not r or not r.get("ok") or r.get("lat") is None:
      return
    t = r["t"]
    if self._last_t == t:
      return
    same = bool(g) and _f(g.get("t", "")) is not None and _f(g.get("t", "")) == _f(t)
    # a phone that sends GGA too: wait for the GGA of the same second (satellites/HDOP/altitude)
    if not same and time.monotonic() - self._gga_mono < 3.0:
      return
    self._last_t = t
    speed = (r["kn"] or 0.0) * KNOTS
    if r["crs"] is not None and speed > 0.5:
      self.bearing = r["crs"]
    hacc = self.gst_hacc if self.gst_hacc else (max(g["hdop"] * 4.0, 3.0) if same and g.get("hdop") else 10.0)
    try:
      d = r["date"]
      hh, mm, ss = int(t[0:2]), int(t[2:4]), float(t[4:])
      unix = calendar.timegm((2000 + int(d[4:6]), int(d[2:4]), int(d[0:2]), hh, mm, int(ss), 0, 0, 0)) + (ss - int(ss))
      time_ok = True
    except (ValueError, IndexError):
      unix, time_ok = _wall(), False
    self.last_fix_mono = time.monotonic()
    self.pending.append({"lat": r["lat"], "lon": r["lon"], "alt": (g.get("alt") if same else None) or 0.0,
                         "speed": speed, "bearing": self.bearing, "hacc": hacc,
                         "sats": g.get("sats", 0) if same else self.sats, "unix": unix, "time_ok": time_ok})

  def fresh(self) -> bool:
    return time.monotonic() - self.last_fix_mono < PHONE_FRESH_S

  def active(self) -> bool:
    """The phone is there: it sent something lately, even without a fix (tunnel)."""
    return self.last_msg_mono > 0 and time.monotonic() - self.last_msg_mono < PHONE_ACTIVE_S


def phone_message(fix: dict):
  dat = messaging.new_message('gpsLocationExternal', valid=True)
  gps = dat.gpsLocationExternal
  gps.source = log.GpsLocationData.SensorSource.android
  gps.flags = 1
  gps.hasFix = True
  gps.latitude = fix["lat"]
  gps.longitude = fix["lon"]
  gps.altitude = fix["alt"]
  gps.speed = fix["speed"]
  gps.bearingDeg = fix["bearing"]
  gps.horizontalAccuracy = fix["hacc"]
  gps.satelliteCount = fix["sats"]
  gps.unixTimestampMillis = int(fix["unix"] * 1e3)
  b = math.radians(fix["bearing"])
  gps.vNED = [fix["speed"] * math.cos(b), fix["speed"] * math.sin(b), 0.0]
  gps.verticalAccuracy = fix["hacc"] * 1.5
  gps.speedAccuracy = 1.0
  gps.bearingAccuracyDeg = 10.0 if fix["speed"] > 2 else 180.0
  return dat


class GpsMux:
  """Decides, per message, whether the comma's own fix or the phone's goes out as gpsLocationExternal."""

  def __init__(self, port: int = PORT):
    self.phone = None
    self.error = ""
    try:
      self.phone = PhoneGps(port)
    except OSError as e:
      self.error = f"can't listen on UDP {PORT}: {e}"
    self.mode = "fused"
    self.mode_checked = 0.0
    self.fusion = GpsFusion()
    self.sm = None
    self.last_out = 0.0
    self.last_int_feed = 0.0
    self.sent_fused = 0
    self.status_written = 0.0
    self.source = "internal"
    self.int_good_since = None
    self.int_bad_since = None
    self.int_fix = False
    self.int_sats = 0
    self.int_hacc = 0.0
    self.int_last = 0.0
    self.int_time_mono = float("-inf")
    self.sent_phone = 0
    # satellite time = time.monotonic() + time_offset. Against the monotonic clock, not the wall clock: timed resets
    # the wall clock from our own output, and an offset kept against the wall clock would chase that reset.
    self.time_offset: float | None = None
    self.time_src = ""

  def phone_active(self) -> bool:
    return self.phone is not None and self.phone.active()

  def _time_reading(self, sat_unix: float, src: str) -> None:
    """A GPS reading was accepted: learn how far satellite time is from the comma's clock. Prefer the comma's own
    GPS; a single far-off reading moves the offset by at most TIME_STEP_MAX_S."""
    now = time.monotonic()
    if src == "phone" and now - self.int_time_mono < 5.0:
      return
    if src == "comma":
      self.int_time_mono = now
    new = sat_unix - now
    if self.time_offset is None:
      self.time_offset = new
    else:
      self.time_offset += max(-TIME_STEP_MAX_S, min(TIME_STEP_MAX_S, new - self.time_offset))
    self.time_src = src

  def sat_time(self) -> float:
    """Best guess of satellite (UTC) time now; the comma's clock until a GPS reading was accepted."""
    return time.monotonic() + self.time_offset if self.time_offset is not None else _wall()

  def _read_mode(self) -> None:
    now = time.monotonic()
    if now - self.mode_checked < 5:
      return
    self.mode_checked = now
    try:
      with open(MODE_PATH) as f:
        m = f.read().strip().lower()
      self.mode = m if m in ("fused", "auto", "phone", "internal") else "fused"
    except OSError:
      self.mode = "fused"

  def internal(self, dat) -> bool:
    """Called with each ublox gpsLocationExternal. True = publish it."""
    g = dat.gpsLocationExternal
    now = time.monotonic()
    self.int_fix, self.int_sats, self.int_hacc, self.int_last = bool(g.hasFix), int(g.satelliteCount), float(g.horizontalAccuracy), now
    good = self.int_fix and self.int_hacc <= INTERNAL_GOOD_HACC and self.int_sats >= INTERNAL_GOOD_SATS
    if good:
      self.int_bad_since = None
      self.int_good_since = self.int_good_since or now
    else:
      self.int_good_since = None
      self.int_bad_since = self.int_bad_since or now
    self._choose()
    if self.mode == "fused":
      # comma's own fix into the filter (1 Hz is plenty; its errors are correlated from one fix to the next). Also while
      # there's no phone, so the filter is ready the moment the phone shows up.
      if self.int_fix and self.int_hacc <= 50.0 and _wall() - self.last_int_feed >= 1.0:
        self.last_int_feed = _wall()
        try:
          if self.fusion.gps(_wall(), g.latitude, g.longitude, g.altitude, max(self.int_hacc, 2.5), g.speed,
                             g.bearingDeg if g.speed > 1.0 else None, g.unixTimestampMillis / 1e3, self.int_sats, "comma"):
            self._time_reading(g.unixTimestampMillis / 1e3, "comma")
        except Exception:
          pass
      # no phone lately: the comma's own fix goes out unchanged, exactly as without this file
      return not self.phone_active()
    return self.source == "internal"

  def _choose(self) -> None:
    self._read_mode()
    if self.mode == "fused":
      self.source = "fused" if self.phone_active() else "internal (no phone)"
      return
    now = time.monotonic()
    phone_ok = self.phone is not None and self.phone.fresh()
    if self.mode == "internal" or not phone_ok:
      self.source = "internal"
    elif self.mode == "phone":
      self.source = "phone"
    elif self.source == "phone":
      # back to the comma's own only once it has been good for a while
      if self.int_good_since and now - self.int_good_since >= INTERNAL_GOOD_HOLD_S:
        self.source = "internal"
    else:
      internal_stale = now - self.int_last > 1.5
      if internal_stale or (self.int_bad_since and now - self.int_bad_since >= INTERNAL_BAD_HOLD_S):
        self.source = "phone"

  def _car(self):
    """(wheel speed m/s, raw gyro (x, y, z) rad/s or None, speed fresh?).
    Speed: starpilotSelfdriveState.vEgo (selfdrived copies carState.vEgo into it, 100 Hz). Gyro: the comma's raw
    gyroscope. Deliberately NOT carState / livePose: msgq allows 15 listeners per message and carState has exactly 15
    while driving - a 16th makes msgq evict everyone over and over ("communication issue")."""
    try:
      if self.sm is None:
        self.sm = messaging.sub_sock('starpilotSelfdriveState', conflate=True)
        self.gyro_sock = messaging.sub_sock('gyroscope', conflate=True)
        self.v = 0.0
        self.v_t = 0.0
        self.g = None
        self.g_t = 0.0
      m = messaging.recv_one_or_none(self.sm)
      if m is not None:
        self.v = float(m.starpilotSelfdriveState.vEgo)
        self.v_t = time.monotonic()
      m = messaging.recv_one_or_none(self.gyro_sock)
      if m is not None:
        ev = m.gyroscope
        g = ev.gyroUncalibrated if ev.which() == 'gyroUncalibrated' else ev.gyro
        if len(g.v) >= 3:
          self.g = (float(g.v[0]), float(g.v[1]), float(g.v[2]))
          self.g_t = time.monotonic()
      now = time.monotonic()
      gyro = self.g if now - self.g_t < 0.5 else None
      return self.v, gyro, now - self.v_t < 0.5
    except Exception:
      return 0.0, None, False

  def _fused_out(self, pm) -> None:
    now = _wall()
    f = self.fusion
    v, yaw, car_ok = self._car()
    f.predict(now, v, yaw, car_ok)
    if now - self.last_out < 0.1 or not f.has_fix(now):
      return
    self.last_out = now
    dat = messaging.new_message('gpsLocationExternal', valid=True)
    gps = dat.gpsLocationExternal
    try:
      gps.source = log.GpsLocationData.SensorSource.fusion
    except Exception:
      gps.source = log.GpsLocationData.SensorSource.android
    gps.flags = 1
    gps.hasFix = True
    gps.latitude = f.lat
    gps.longitude = f.lon
    gps.altitude = f.alt
    gps.speed = f.speed
    gps.bearingDeg = f.heading
    gps.horizontalAccuracy = f.hacc()
    gps.satelliteCount = 0 if f.dead_reckoning(now) else f.sats
    gps.unixTimestampMillis = int(self.sat_time() * 1e3)   # satellite time, so timed can still correct a wrong clock
    b = math.radians(f.heading)
    gps.vNED = [f.speed * math.cos(b), f.speed * math.sin(b), 0.0]
    gps.verticalAccuracy = f.hacc() * 1.5
    gps.speedAccuracy = 0.3 if car_ok else 1.0
    gps.bearingAccuracyDeg = f.sig_h
    pm.send('gpsLocationExternal', dat)
    self.sent_fused += 1

  def poll(self, pm) -> None:
    self._read_mode()
    if self.mode == "fused":
      if self.phone is not None:
        for fix in self.phone.poll():
          crs = fix["bearing"] if fix["speed"] > 1.0 else None
          if self.fusion.gps(_wall(), fix["lat"], fix["lon"], fix["alt"], fix["hacc"], fix["speed"], crs, fix["unix"],
                             fix["sats"], "phone") and fix.get("time_ok"):
            self._time_reading(fix["unix"], "phone")
      self._choose()
      if self.phone_active():
        self._fused_out(pm)
      self._status()
      return
    if self.phone is not None:
      fixes = self.phone.poll()
      self._choose()
      if self.source == "phone":
        for fix in fixes:
          pm.send('gpsLocationExternal', phone_message(fix))
          self.sent_phone += 1
    self._status()

  def _status(self) -> None:
    now = time.monotonic()
    if now - self.status_written < 1.0:
      return
    self.status_written = now
    p = self.phone
    st = {"mode": self.mode, "source": self.source, "error": self.error,
          "phone": {"fresh": bool(p and p.fresh()), "ageS": round(now - p.last_fix_mono, 1) if p and p.last_fix_mono else None,
                    "sats": p.sats if p else 0, "from": p.sender if p else "", "packets": p.packets if p else 0,
                    "published": self.sent_phone, "active": self.phone_active(),
                    "allowed": sorted(p.allowed) if p else [], "rejected": p.rejected if p else 0,
                    "rejectedFrom": dict(p.rejected_from) if p else {}},
          "time": {"source": self.time_src, "offsetS": round(self.time_offset + time.monotonic() - _wall(), 1)
                   if self.time_offset is not None else None},
          "fused": dict(self.fusion.status(_wall()), published=self.sent_fused) if self.mode == "fused" else None,
          "internal": {"fix": self.int_fix, "sats": self.int_sats, "hAccM": round(self.int_hacc, 1),
                       "ageS": round(now - self.int_last, 1) if self.int_last else None},
          "t": _wall()}
    try:
      tmp = STATUS_PATH + ".tmp"
      with open(tmp, "w") as f:
        json.dump(st, f)
      os.replace(tmp, STATUS_PATH)
    except OSError:
      pass
