"""Waze <-> StarPilot navigation bridge (hybrid, phase 1).

  tablet (StarView, Waze reader) --POST /waze--> starviewd: WazeFeed.ingest()
     - parses the snapshot, writes /dev/shm/starview_waze.json (+ recordings for tuning)
     - Waze shows a route + its destination -> geocoded (Mapbox Search, near the GPS position) and set as NavDestination,
       so navigationd starts and the Mapbox route (step list, lanes, lookahead) is ready from the start of the drive.
       Waze's driving screen doesn't show the destination: it's on the bottom sheet, which the reader opens for a
       moment when the reply says needDestination (StarView 2.20+), or you open it by tapping the bottom bar.
     - Waze ends the route (on screen, not navigating, 15 s) -> a destination this bridge set is cleared again.
     This lives in starviewd, not navigationd: navigationd only runs once a destination exists.
  navigationd: WazeHybrid.state_fields() compares Waze's next maneuver with the Mapbox route's every second
     (street / distance / direction) -> NavInstructionState waze* fields + log.
  Phase 2 (after looking at recordings): on lasting disagreement Waze wins (reroute Mapbox through Waze's road).

Off switch on the comma: touch /data/starview/no_waze  (the tablet also has its own switch).
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import threading
import time
from typing import Any

from openpilot.starpilot.navigation import waze_parser


def _wall() -> float:
  return time.time()  # noqa: TID251  wall clock on purpose: compared with GPS UTC times / shared through files


STATE_PATH = "/dev/shm/starview_waze.json"
LOG_DIR = "/data/media/0/waze_logs"
LOG_CAP_BYTES = 300 * 1024 * 1024
OUR_DEST_PATH = "/data/starview/waze_destination.json"
OFF_FLAG = "/data/starview/no_waze"
FRESH_S = 3.0
GEOCODE_URL = "https://api.mapbox.com/search/searchbox/v1/forward"


def _atomic_write(path: str, text: str) -> None:
  os.makedirs(os.path.dirname(path), exist_ok=True)
  tmp = f"{path}.tmp{os.getpid()}"
  with open(tmp, "w") as f:
    f.write(text)
  os.replace(tmp, path)


def _json_param(p, key: str) -> Any:
  try:
    raw = p.get(key)
  except Exception:
    return None
  if isinstance(raw, (bytes, str)):
    try:
      return json.loads(raw)
    except Exception:
      return raw.decode() if isinstance(raw, bytes) else raw
  return raw


END_CLEAR_S = 15.0
GEOCODE_RETRY_S = 60.0   # address not found
NET_RETRY_S = 15.0       # network error


def _read_json(path: str) -> Any:
  try:
    with open(path) as f:
      return json.load(f)
  except Exception:
    return None


def _warn(msg: str) -> None:
  try:
    from openpilot.common.swaglog import cloudlog
    cloudlog.warning(f"waze: {msg}")
  except Exception:
    pass


def _mapbox_keys(params) -> list[str]:
  keys = []
  for k in ("MapboxPublicKey", "MapboxSecretKey"):
    try:
      v = params.get(k, encoding="utf-8") or ""
    except Exception:
      v = ""
    if v:
      keys.append(v)
  return keys


def geocode(text: str, keys: list[str], pos: tuple[float, float] | None, session=None) -> tuple[float, float, str] | None:
  """(lon, lat, name) of the best Mapbox Search Box match for an address, near pos (lon, lat) if given."""
  import requests
  sess = session or requests
  for key in keys:
    q = {"q": text, "limit": 1, "access_token": key}
    if pos is not None:
      q["proximity"] = f"{pos[0]},{pos[1]}"
    r = sess.get(GEOCODE_URL, params=q, timeout=5)
    if r.status_code != 200:
      continue
    feats = (r.json() or {}).get("features") or []
    if feats:
      lon, lat = feats[0]["geometry"]["coordinates"][:2]
      props = feats[0].get("properties") or {}
      return float(lon), float(lat), str(props.get("full_address") or props.get("name") or text)
  return None


# ------------------------------------------------------------------ starviewd side
class WazeFeed:
  """Receives tablet snapshots: parses, publishes the latest for navigationd, keeps recordings for tuning."""

  def __init__(self):
    self._lock = threading.Lock()
    self._last_hash = ""
    self._last_log = 0.0
    self._last: dict[str, Any] = {}
    self._count = 0
    self._params = self._params_mem = None
    self._session = None
    # current Waze route
    self.route_dest = ""          # destination text read for the route Waze is on now (kept after the sheet closes)
    self.route_label = ""
    self._handled_dest = ""       # destination already turned into NavDestination
    self._geocode_inflight = False
    self._geocode_failed_at = 0.0
    self._geocode_failed_for = ""
    self._retry_s = GEOCODE_RETRY_S
    self._not_nav_since: float | None = None
    self.last_handoff = ""

  def _p(self):
    if self._params is None:
      from openpilot.common.params import Params
      self._params, self._params_mem = Params(), Params(memory=True)
    return self._params, self._params_mem

  def ingest(self, snap: dict[str, Any]) -> dict[str, Any]:
    now = _wall()
    parsed = waze_parser.parse(snap)
    off = os.path.exists(OFF_FLAG)
    nav_state = None
    try:
      params, mem = self._p()
      nav_state = _json_param(mem, "NavInstructionState")
      if not off:
        self._route_tracking(parsed, params, mem)
      if snap.get("record"):
        self._record(snap, parsed, now, params, mem, nav_state)
    except Exception as e:
      parsed["error"] = str(e)[:200]
    if parsed.get("navigating") and not parsed.get("destination") and self.route_dest:
      parsed["destination"], parsed["destinationLabel"] = self.route_dest, self.route_label
      parsed["via"].append("destination:remembered")
    state = {"receivedAt": now, "tabletT": snap.get("t"), "app": snap.get("app", ""), "parsed": parsed}
    _atomic_write(STATE_PATH, json.dumps(state))
    cmp = waze_parser.compare(parsed, nav_state if isinstance(nav_state, dict) else None)
    if isinstance(nav_state, dict) and nav_state.get("wazeReroute"):
      cmp["wazeReroute"] = str(nav_state["wazeReroute"])     # set by navigationd when Waze wins
    need = bool(parsed.get("navigating")) and not self.route_dest and not off
    with self._lock:
      self._count += 1
      self._last = {"at": now, "parsed": parsed, "compare": cmp, "count": self._count, "handoff": self.last_handoff}
    return {"ok": True, "parsed": parsed, "compare": cmp, "off": off, "needDestination": need, "handoff": self.last_handoff}

  # ---- destination handoff (runs in starviewd, which is always up)
  def _route_tracking(self, parsed: dict[str, Any], params, mem) -> None:
    if not parsed.get("present"):
      return                                   # Waze not on screen: no news, keep everything
    now = time.monotonic()
    if parsed.get("navigating") or parsed.get("sheetOpen"):
      self._not_nav_since = None
      dest = str(parsed.get("destination") or "").strip()
      if dest and dest != self.route_dest:
        self.route_dest, self.route_label = dest, str(parsed.get("destinationLabel") or "")
      dest = dest or self.route_dest          # the sheet is closed again: keep trying with what was read
      if dest and dest != self._handled_dest and not self._geocode_inflight and \
         not (dest == self._geocode_failed_for and _wall() - self._geocode_failed_at < self._retry_s):
        self._start_geocode(dest, params, mem)
      return
    self._not_nav_since = self._not_nav_since or now
    if now - self._not_nav_since < END_CLEAR_S:
      return
    from openpilot.starpilot.navigation.destination_store import parse_destination_json, same_destination
    current = parse_destination_json(params.get("NavDestination", encoding="utf-8"))
    ours = _read_json(OUR_DEST_PATH)
    if current and ours and same_destination(current, ours):
      params.remove("NavDestination")
      self.last_handoff = "Waze route ended: destination cleared"
      _warn(self.last_handoff)
    if self.route_dest or self._handled_dest:
      self.route_dest = self.route_label = self._handled_dest = ""
      self.last_handoff = ""
      try:
        os.unlink(OUR_DEST_PATH)
      except OSError:
        pass
    self._not_nav_since = None

  def _start_geocode(self, text: str, params, mem) -> None:
    keys = _mapbox_keys(params)
    if not keys:
      self.last_handoff = "no Mapbox key on the comma (Galaxy > Navigation)"
      return
    gps = _json_param(mem, "LastGPSPosition")
    pos = (gps["longitude"], gps["latitude"]) if isinstance(gps, dict) and gps.get("hasFix") else None
    self._geocode_inflight = True

    def worker():
      try:
        hit = geocode(text, keys, pos, self._session)
        if hit is None:
          self._geocode_failed_at, self._geocode_failed_for, self._retry_s = _wall(), text, GEOCODE_RETRY_S
          self.last_handoff = f"could not find {text!r}"
          _warn(self.last_handoff)
          return
        lon, lat, name = hit
        from openpilot.starpilot.navigation.destination_store import set_navigation_destination
        label = f"{self.route_label}: {name}" if self.route_label else name
        dest = set_navigation_destination(params, {"place_name": label, "latitude": lat, "longitude": lon}, skip_if_same=True)
        if dest:
          _atomic_write(OUR_DEST_PATH, json.dumps(dest))
          self._handled_dest = text
          self.last_handoff = f"destination set: {label}"
          _warn(f"{self.last_handoff} ({lat:.5f},{lon:.5f})")
      except Exception as e:
        # network trouble (no internet yet, DNS): retry soon; never show the URL (it carries the access token)
        self._geocode_failed_at, self._geocode_failed_for, self._retry_s = _wall(), text, NET_RETRY_S
        msg = str(e)
        if "resolution" in msg or "resolve" in msg:
          why = "no internet on the comma (DNS failed)"
        elif "timed out" in msg.lower() or "timeout" in msg.lower():
          why = "Mapbox timed out"
        elif "Connection" in msg:
          why = "can't connect to Mapbox (no internet?)"
        else:
          why = type(e).__name__
        self.last_handoff = f"{why}: retrying in {int(NET_RETRY_S)} s"
        _warn(f"geocode {text!r}: {why}")
        _warn(self.last_handoff)
      finally:
        self._geocode_inflight = False

    threading.Thread(target=worker, daemon=True).start()

  def status(self) -> dict[str, Any]:
    with self._lock:
      return dict(self._last)

  def _record(self, snap, parsed, now, params, mem, nav_state) -> None:
    nodes_hash = hashlib.sha1(json.dumps(snap.get("nodes") or [], sort_keys=True).encode()).hexdigest()[:16]
    shot = snap.get("shot")
    if nodes_hash == self._last_hash and now - self._last_log < 10.0 and not shot:
      return
    self._last_hash, self._last_log = nodes_hash, now
    os.makedirs(LOG_DIR, exist_ok=True)
    day = time.strftime("%Y%m%d", time.localtime(now))
    rec = {"t": now, "hash": nodes_hash, "snap": {k: v for k, v in snap.items() if k != "shot"}, "parsed": parsed,
           "gps": _json_param(mem, "LastGPSPosition"), "navState": nav_state,
           "navDestination": _json_param(params, "NavDestination")}
    if shot:
      try:
        os.makedirs(f"{LOG_DIR}/shots", exist_ok=True)
        name = f"shots/{int(now * 1000)}.jpg"
        with open(f"{LOG_DIR}/{name}", "wb") as f:
          f.write(base64.b64decode(shot))
        rec["shot"] = name
      except Exception:
        pass
    with open(f"{LOG_DIR}/waze-{day}.jsonl", "a") as f:
      f.write(json.dumps(rec) + "\n")
    self._trim()

  def _trim(self) -> None:
    files = []
    for root, _, names in os.walk(LOG_DIR):
      for n in names:
        p = os.path.join(root, n)
        try:
          st = os.stat(p)
          files.append((st.st_mtime, st.st_size, p))
        except OSError:
          pass
    total = sum(f[1] for f in files)
    for _, size, p in sorted(files):
      if total <= LOG_CAP_BYTES:
        break
      try:
        os.unlink(p)
        total -= size
      except OSError:
        pass


def load_state(max_age: float = FRESH_S) -> dict[str, Any] | None:
  try:
    with open(STATE_PATH) as f:
      st = json.load(f)
  except Exception:
    return None
  if _wall() - float(st.get("receivedAt", 0)) > max_age:
    return None
  return st


# ------------------------------------------------------------------ navigationd side
def project(lat: float, lon: float, bearing_deg: float, dist_m: float) -> tuple[float, float]:
  """Point dist_m ahead of (lat, lon) along bearing_deg (flat-earth, fine for a few km)."""
  b = math.radians(bearing_deg)
  dlat = dist_m * math.cos(b) / 111_320.0
  dlon = dist_m * math.sin(b) / (111_320.0 * max(math.cos(math.radians(lat)), 1e-6))
  return lat + dlat, lon + dlon


def meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
  dy = (lat2 - lat1) * 111_320.0
  dx = (lon2 - lon1) * 111_320.0 * math.cos(math.radians((lat1 + lat2) / 2))
  return math.hypot(dx, dy)


def find_street(name: str, keys: list[str], near: tuple[float, float], session=None) -> tuple[float, float] | None:
  """(lat, lon) of the street called `name` closest to `near` (lat, lon), via Mapbox Search (types=street)."""
  import requests
  sess = session or requests
  for key in keys:
    r = sess.get(GEOCODE_URL, params={"q": name, "types": "street", "proximity": f"{near[1]},{near[0]}", "limit": 1,
                                      "access_token": key}, timeout=5)
    if r.status_code != 200:
      continue
    feats = (r.json() or {}).get("features") or []
    if feats:
      lon, lat = feats[0]["geometry"]["coordinates"][:2]
      return float(lat), float(lon)
  return None


class WazeHybrid:
  """navigationd side: Waze vs Mapbox comparison, and "Waze wins": when they keep disagreeing, StarPilot's route is
  recomputed through a via point on the street Waze turns onto, so turn slowdowns, turn desires and lane positioning
  follow Waze's road. (The destination handoff is in WazeFeed / starviewd.)"""
  DISAGREE_LOG_S = 10.0
  WAZE_WINS_S = 6.0          # disagreement this long (and moving) -> reroute through Waze's road
  RETRY_SAME_STREET_S = 60.0
  MIN_MANEUVER_M = 40.0      # too close to the turn: rerouting can't help any more

  def __init__(self, params, params_memory, session=None, log=None):
    self.params, self.params_memory = params, params_memory
    self._log = log
    self._waze: dict[str, Any] = {}
    self._fresh = False
    self._disagree_since: float | None = None
    self._disagree_logged_at = 0.0
    self._session = session
    self._tried: dict[str, float] = {}      # Waze street -> monotonic time of the last reroute attempt
    self._via_inflight = False
    self._pending_via: tuple[float, float] | None = None
    self.reroute_status = ""

  def _warn(self, msg: str) -> None:
    if self._log is not None:
      self._log.warning(f"waze: {msg}")

  @property
  def enabled(self) -> bool:
    return not os.path.exists(OFF_FLAG)

  def update(self, last_position=None) -> None:
    """Call once per navigationd tick."""
    st = load_state() if self.enabled else None
    self._fresh = st is not None
    self._waze = (st or {}).get("parsed") or {}

  def take_via(self) -> tuple[float, float] | None:
    """A via point (lat, lon) navigationd should reroute through now, if Waze won a disagreement."""
    via, self._pending_via = self._pending_via, None
    return via

  def maybe_waze_wins(self, position, bearing: float | None, v_ego: float) -> None:
    """Call once per tick after state_fields(): starts the via-point lookup when the disagreement has lasted."""
    if not self._fresh or self._disagree_since is None or self._via_inflight or position is None or bearing is None:
      return
    now = time.monotonic()
    street = str(self._waze.get("street") or "").strip()
    dist = self._waze.get("distanceM")
    if not street or dist is None or float(dist) < self.MIN_MANEUVER_M or v_ego < 1.0:
      return
    if now - self._disagree_since < self.WAZE_WINS_S or now - self._tried.get(street, -1e9) < self.RETRY_SAME_STREET_S:
      return
    keys = _mapbox_keys(self.params)
    if not keys:
      return
    self._tried[street] = now
    self._via_inflight = True
    lat0, lon0 = float(position.latitude), float(position.longitude)
    near = project(lat0, lon0, float(bearing), float(dist))   # roughly where Waze's turn is
    self.reroute_status = f"following Waze: looking up {street}"

    def worker():
      try:
        limit = max(800.0, 0.5 * float(dist))
        found_far = False
        # a Waze signpost names several roads ("to Kennedy Airport / Belt Pkwy W / I-678 ..."): try each road
        for name in waze_parser.road_names(street)[:3]:
          hit = find_street(name, keys, near, self._session)
          if hit is None:
            continue
          # sanity: the street point must be near where Waze says the turn is
          if meters(hit[0], hit[1], near[0], near[1]) > limit:
            found_far = True
            continue
          self._pending_via = hit
          self.reroute_status = f"rerouting via {name} (Waze's road)"
          self._warn(f"{self.reroute_status} at {hit[0]:.5f},{hit[1]:.5f}")
          return
        short = waze_parser.road_names(street)[0][:40]
        self.reroute_status = f"{short} found too far from Waze's turn" if found_far else f"{short} not found on the map"
        self._warn(f"via lookup for {street!r}: {self.reroute_status}")
      except Exception as e:
        self.reroute_status = f"reroute lookup failed: {type(e).__name__}"
        self._warn(f"via lookup: {e}")
      finally:
        self._via_inflight = False

    threading.Thread(target=worker, daemon=True).start()

  def state_fields(self, mapbox_state: dict[str, Any], upcoming: list[tuple[str, float | None]] | None = None) -> dict[str, Any]:
    """Extra NavInstructionState fields (consumers ignore unknown keys). upcoming: (text, distance) of the next steps."""
    if not self._fresh:
      self._disagree_since = None
      return {"wazeFresh": False}
    res = waze_parser.compare(self._waze, mapbox_state, upcoming)
    res["wazeFresh"] = True
    now = time.monotonic()
    if res["wazeAgree"] is False:
      self._disagree_since = self._disagree_since or now
      if now - self._disagree_since > self.DISAGREE_LOG_S and now - self._disagree_logged_at > self.DISAGREE_LOG_S:
        self._disagree_logged_at = now
        mb = f"mapbox {mapbox_state.get('maneuverPrimaryText')!r} {mapbox_state.get('maneuverDistance'):.0f} m"
        self._warn(f"disagree {now - self._disagree_since:.0f}s: waze {res['wazeStreet']!r} {res['wazeDistance']} m vs {mb}")
    else:
      self._disagree_since = None
    res["wazeDisagreeS"] = round(now - self._disagree_since, 1) if self._disagree_since else 0.0
    if self.reroute_status:
      res["wazeReroute"] = self.reroute_status
    if res["wazeAgree"]:
      self.reroute_status = ""
    return res
