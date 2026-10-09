#!/usr/bin/env python3
"""
GPS + car sensors fusion (StarView). Carries the position forward between GPS fixes with the car's wheel speed
(starpilotSelfdriveState.vEgo = carState.vEgo) and the comma's raw gyroscope, and pulls it back toward each GPS fix weighted by accuracy:
  - 10 Hz output from a 1 Hz phone
  - keeps moving through tunnels / under els / between tall buildings (dead reckoning, accuracy grows honestly)
  - GPS jumps (Manhattan multipath) far outside the expected error are rejected, unless they keep repeating
  - steady heading at low speed (gyro instead of the noisy GPS course)
GPS fixes arrive late (the phone's NMEA can be ~0.3-1 s old): each fix is compared with where the filter was at the
fix's own time (short history), and the correction is applied to now.
Which gyro axis is "turning" and its sign aren't assumed: they are learned by comparing each axis's turning with GPS
course changes, and the gyro is only used once that's settled (kept in /data/starview/gyro_axis as "axis sign").
"""
import math
from collections import deque

M_PER_DEG = 111320.0
SIGN_PATH = "/data/starview/gyro_axis"
MAX_DR_S = 600.0            # stop claiming a fix after this long / this far with no accepted GPS
MAX_DR_M = 4000.0
HIST_S = 3.0


def wrap180(a: float) -> float:
  return (a + 180.0) % 360.0 - 180.0


class GpsFusion:
  def __init__(self):
    self.init = False
    self.lat = self.lon = 0.0
    self.alt = 0.0
    self.heading = 0.0          # deg, clockwise from north
    self.heading_ok = False
    self.P = 100.0              # position variance per axis (m^2)
    self.sig_h = 30.0           # heading std (deg)
    self.speed = 0.0
    self.scale = 1.0            # GPS speed / wheel speed
    self.bias = [0.0, 0.0, 0.0]  # gyro bias per axis (rad/s), learned while stopped
    self.axis, self.sign = self._load_sign()
    self.score = [0.0, 0.0, 0.0]
    self.votes = [0.0, 0.0, 0.0]
    self.t_pred = None
    self.hist: deque = deque()
    self.last_gps_wall = 0.0
    self.dr_dist = 0.0
    self.sats = 0
    self.rejects = 0
    self.accepted = 0
    self.rejected = 0
    self.last_gps_course = None  # (wall t, course deg, gyro integral at that time)
    self.gyro_int = [0.0, 0.0, 0.0]  # integrated raw gyro per axis (deg), for axis/sign learning
    self.last_src = ""
    self.gps_speed = 0.0
    self.last_course_t = None

  # ---------- persistence ----------
  def _load_sign(self):
    try:
      with open(SIGN_PATH) as f:
        a, s = (int(x) for x in f.read().split())
      return (a, s) if a in (0, 1, 2) and s in (-1, 1) else (2, 0)
    except (OSError, ValueError):
      return 2, 0

  def _save_sign(self) -> None:
    try:
      with open(SIGN_PATH, "w") as f:
        f.write(f"{self.axis} {self.sign}")
    except OSError:
      pass

  # ---------- prediction ----------
  def predict(self, now: float, v_ego: float, gyro, car_ok: bool) -> None:
    """gyro: raw (x, y, z) rad/s from the comma's gyroscope, or None."""
    if self.t_pred is None:
      self.t_pred = now
      return
    dt = now - self.t_pred
    self.t_pred = now
    if dt <= 0 or dt > 1.0:
      return
    yaw_rate_raw = None
    if gyro is not None:
      for i in range(3):
        if car_ok and abs(v_ego) < 0.05:
          self.bias[i] += 0.02 * (gyro[i] - self.bias[i])     # standing still: whatever the gyro says is bias
        self.gyro_int[i] += math.degrees(gyro[i] - self.bias[i]) * dt
      yaw_rate_raw = gyro[self.axis] - self.bias[self.axis]
    if not self.init:
      return
    v = max(v_ego, 0.0) * self.scale if car_ok else self.gps_speed
    self.speed = v
    if self.sign != 0 and yaw_rate_raw is not None and car_ok:
      self.heading = (self.heading + self.sign * math.degrees(yaw_rate_raw) * dt) % 360.0
      self.sig_h = min(self.sig_h + 0.05 * dt, 45.0)           # gyro drift
    elif v > 0.5:
      self.sig_h = min(self.sig_h + 2.0 * dt, 45.0)            # no gyro: heading only from GPS
    d = v * dt
    if d > 0:
      h = math.radians(self.heading)
      self.lat += d * math.cos(h) / M_PER_DEG
      self.lon += d * math.sin(h) / (M_PER_DEG * max(math.cos(math.radians(self.lat)), 0.2))
      self.dr_dist += d
      self.P += ((0.03 * v) ** 2 + (v * math.radians(self.sig_h)) ** 2 * 0.25) * dt
    self.hist.append((now, self.lat, self.lon, self.heading))
    while self.hist and now - self.hist[0][0] > HIST_S:
      self.hist.popleft()

  def _at(self, t: float):
    """Filter state at wall time t (from the history), or the current one."""
    best = None
    for item in self.hist:
      if item[0] <= t:
        best = item
      else:
        break
    return best or (self.t_pred or t, self.lat, self.lon, self.heading)

  # ---------- correction ----------
  def gps(self, now: float, lat: float, lon: float, alt: float, hacc: float, speed: float, course: float | None,
          fix_time: float, sats: int, src: str) -> bool:
    """One GPS fix. Returns True if it was used."""
    hacc = max(hacc, 2.0)
    lag = now - fix_time
    if not (-0.5 <= lag <= 2.5):
      lag = 0.4 if src == "phone" else 0.05
    t_fix = now - max(lag, 0.0)
    self.sats = sats
    self.alt = alt
    self.last_src = src
    if not self.init:
      self.lat, self.lon, self.P = lat, lon, hacc ** 2
      if course is not None and speed > 3:
        self.heading, self.heading_ok, self.sig_h = course, True, 10.0
      self.init = True
      self.gps_speed = speed
      self._accepted(now)
      return True

    _, plat, plon, phead = self._at(t_fix)
    dn = (lat - plat) * M_PER_DEG
    de = (lon - plon) * M_PER_DEG * max(math.cos(math.radians(lat)), 0.2)
    dist = math.hypot(dn, de)
    R = hacc ** 2
    gate = max(4.0 * math.sqrt(self.P + R), 30.0)
    stale = (now - self.last_gps_wall) > 30.0
    if dist > gate and not stale:
      self.rejects += 1
      self.rejected += 1
      if self.rejects < 4:          # one-off jump (multipath): ignore. Several in a row: the filter is the one wrong
        return False
      K = 1.0
    else:
      K = self.P / (self.P + R) if not stale else 1.0
    self.rejects = 0
    self._shift(K * dn / M_PER_DEG, K * de / (M_PER_DEG * max(math.cos(math.radians(self.lat)), 0.2)), 0.0)
    self.P = R if K >= 1.0 else max((1 - K) * self.P, 1.0)
    self.gps_speed = speed

    # heading from GPS course when moving fast enough for it to mean something
    if course is not None and speed > 5.0:
      sig_c = 4.0 if speed > 12 else 8.0
      if not self.heading_ok or self.sign == 0:
        Kh = 0.6 if self.heading_ok else 1.0
      else:
        Kh = self.sig_h ** 2 / (self.sig_h ** 2 + sig_c ** 2)
      innov = wrap180(course - phead)
      self._shift(0.0, 0.0, Kh * innov)
      # gyro bias while driving: a heading error that keeps building up between fixes in one direction is bias
      if self.sign != 0 and self.last_course_t is not None and abs(innov) < 10.0:
        gap = now - self.last_course_t
        if 0.3 < gap < 3.0:
          b = self.bias[self.axis] - 0.003 * self.sign * math.radians(innov) / gap
          self.bias[self.axis] = max(-0.02, min(0.02, b))
      self.last_course_t = now
      self.sig_h = max(math.sqrt(1 - Kh) * self.sig_h, 1.0)
      self.heading_ok = True
      self._learn_sign(now, course)
    # wheel-speed scale (tire size / speedometer offset)
    if speed > 8.0 and self.speed > 8.0 and self.scale > 0:
      ratio = speed / (self.speed / self.scale)
      if 0.8 < ratio < 1.25:
        self.scale += 0.02 * (ratio - self.scale)
    self._accepted(now)
    return True

  def _shift(self, dlat: float, dlon: float, dhead: float) -> None:
    """Apply a correction to now AND to the history, so the next (late) fix isn't compared with uncorrected past."""
    self.lat += dlat
    self.lon += dlon
    self.heading = (self.heading + dhead) % 360.0
    self.hist = deque((t, la + dlat, lo + dlon, (h + dhead) % 360.0) for t, la, lo, h in self.hist)

  def _accepted(self, now: float) -> None:
    self.accepted += 1
    self.last_gps_wall = now
    self.dr_dist = 0.0

  def _learn_sign(self, now: float, course: float) -> None:
    prev = self.last_gps_course
    self.last_gps_course = (now, course, list(self.gyro_int))
    if prev is None:
      return
    dt = now - prev[0]
    if dt > 3.0:
      return
    turn_gps = wrap180(course - prev[1])
    if abs(turn_gps) < 4.0:
      return
    # the turning axis turns about as much as GPS says (|ratio| ~ 1); the other two barely move
    for i in range(3):
      r = (self.gyro_int[i] - prev[2][i]) / turn_gps
      if 0.6 < abs(r) < 1.5:
        self.score[i] = min(self.score[i] + 1.0, 30.0)
        self.votes[i] = max(-20.0, min(20.0, self.votes[i] + (1.0 if r > 0 else -1.0)))
      else:
        self.score[i] = max(self.score[i] - 0.5, -10.0)
    best = max(range(3), key=lambda i: self.score[i])
    others = max(self.score[i] for i in range(3) if i != best)
    if self.score[best] >= 6 and self.score[best] - others >= 3 and abs(self.votes[best]) >= 4:
      new = (best, 1 if self.votes[best] > 0 else -1)
      if new != (self.axis, self.sign):
        self.axis, self.sign = new
        self._save_sign()

  # ---------- output ----------
  def has_fix(self, now: float) -> bool:
    return self.init and (now - self.last_gps_wall) < MAX_DR_S and self.dr_dist < MAX_DR_M

  def dead_reckoning(self, now: float) -> bool:
    return self.init and (now - self.last_gps_wall) > 2.5

  def hacc(self) -> float:
    return math.sqrt(max(self.P, 1.0))

  def status(self, now: float) -> dict:
    return {"init": self.init, "fix": self.has_fix(now), "deadReckoning": self.dead_reckoning(now),
            "sinceGpsS": round(now - self.last_gps_wall, 1) if self.last_gps_wall else None,
            "drDistM": round(self.dr_dist), "hAccM": round(self.hacc(), 1), "headingStdDeg": round(self.sig_h, 1),
            "gyro": f"learned: axis {'xyz'[self.axis]} sign {self.sign:+d}" if self.sign else "learning (drive a few turns)",
            "wheelScale": round(self.scale, 3), "accepted": self.accepted, "rejectedJumps": self.rejected,
            "lastGps": self.last_src}
