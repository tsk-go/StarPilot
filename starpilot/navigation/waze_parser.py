"""Turn a raw Waze screen snapshot (sent by the StarView tablet's accessibility reader) into navigation fields.

The tablet sends Waze's view tree as-is; all interpretation happens here, so the parser can be improved with a
`git pull` instead of a new APK. Snapshot format (StarView >= 2.17.0):

  {"v": 1, "t": <tablet epoch ms>, "w": <screen px>, "h": <screen px>, "present": bool,
   "nodes": [{"i": "<view id without 'com.waze:id/'>", "c": "<class simple name>", "t": "<text>",
              "d": "<content description>", "b": [left, top, right, bottom]}, ...]}

Waze's view ids and layout are not documented and change between versions, so this is deliberately two-layered:
known id keywords first (filled in once real recordings show them), then layout heuristics (distance-looking text
near the top = next maneuver, clock / minutes near the bottom = ETA).
"""
from __future__ import annotations

import re
from typing import Any

FT, MI, YD = 0.3048, 1609.344, 0.9144

DIST_RE = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*(ft|feet|mi|mile|miles|m|km|yd|yds)\.?\s*$", re.I)
UNIT_RE = re.compile(r"^\s*(ft|feet|mi|mile|miles|m|km|yd|yds)\.?\s*$", re.I)
NUM_RE = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*$")
CLOCK_RE = re.compile(r"^\s*(\d{1,2}:\d{2})\s*([ap]\.?\s?m\.?)?\s*$", re.I)
MINUTES_RE = re.compile(r"^\s*(?:(\d+)\s*(?:h|hr|hrs|hour|hours)\s*)?(?:(\d+)\s*(?:min|mins|minutes))?\s*$", re.I)

# id keyword -> field. Matched as substrings of the lower-cased view id.
ID_HINTS = {
  "distance": ("distance", "dist"),
  "street": ("street", "instruction", "road_name", "roadname", "nextstreet", "next_street", "title"),
  "then": ("then", "next_next", "secondary"),
  "eta": ("eta", "arrival"),
  "remaining": ("remaining", "time_left", "timeleft", "duration"),
  "destination": ("destination", "address", "dest"),
}

NOISE = {"then", "eta", "arrival", "min", "mins", "mi", "km", "ft", "m", "go", "ok", "cancel", "stop", "report", "search",
         "my waze", "menu", "mute", "unmute", "overview", "resume", "recenter", "add a stop", "steps"}


def _to_m(value: str, unit: str) -> float:
  v = float(value.replace(",", "."))
  u = unit.lower().rstrip(".")
  if u in ("ft", "feet"):
    return v * FT
  if u in ("mi", "mile", "miles"):
    return v * MI
  if u in ("yd", "yds"):
    return v * YD
  if u == "km":
    return v * 1000.0
  return v


def _norm_nodes(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
  out = []
  for n in snapshot.get("nodes") or []:
    if not isinstance(n, dict):
      continue
    b = n.get("b") or [0, 0, 0, 0]
    if not isinstance(b, (list, tuple)) or len(b) != 4:
      b = [0, 0, 0, 0]
    out.append({
      "i": str(n.get("i") or "").lower(),
      "c": str(n.get("c") or ""),
      "t": str(n.get("t") or "").strip(),
      "d": str(n.get("d") or "").strip(),
      "b": [float(x or 0) for x in b],
    })
  return out


def _text(n: dict[str, Any]) -> str:
  return n["t"] or n["d"]


def _distances(nodes: list[dict[str, Any]]) -> list[tuple[dict[str, Any], float, str]]:
  """Every distance shown on screen, including '0.3' + 'mi' split over two neighbouring views."""
  found = []
  for idx, n in enumerate(nodes):
    t = _text(n)
    m = DIST_RE.match(t)
    if m:
      found.append((n, _to_m(m.group(1), m.group(2)), t))
      continue
    m = NUM_RE.match(t)
    if m and idx + 1 < len(nodes):
      nxt = nodes[idx + 1]
      u = UNIT_RE.match(_text(nxt))
      if u and abs(nxt["b"][1] - n["b"][1]) < max(40.0, n["b"][3] - n["b"][1]):
        found.append((n, _to_m(m.group(1), u.group(1)), f"{t} {_text(nxt)}"))
  return found


def _by_id(nodes: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
  keys = ID_HINTS[field]
  return [n for n in nodes if n["i"] and any(k in n["i"] for k in keys) and _text(n)]


MODIFIERS = [
  ("u-turn", "uturn"), ("u turn", "uturn"), ("make a u", "uturn"),
  ("sharp left", "sharpLeft"), ("sharp right", "sharpRight"),
  ("slight left", "slightLeft"), ("slight right", "slightRight"),
  ("bear left", "slightLeft"), ("bear right", "slightRight"),
  ("keep left", "slightLeft"), ("keep right", "slightRight"),
  ("exit left", "slightLeft"), ("exit right", "slightRight"),
  ("turn left", "left"), ("turn right", "right"),
  ("left", "left"), ("right", "right"),
  ("straight", "straight"), ("continue", "straight"),
]


def maneuver_from_text(text: str) -> tuple[str, str]:
  """(maneuverType, modifier) in Mapbox terms from an instruction or an arrow's content description."""
  t = text.lower()
  mtype = "turn"
  if "roundabout" in t or "rotary" in t:
    mtype = "roundabout"
  elif "exit" in t or "ramp" in t:
    mtype = "off ramp"
  elif "keep" in t or "fork" in t:
    mtype = "fork"
  elif "arriv" in t or "destination" in t:
    mtype = "arrive"
  elif "merge" in t:
    mtype = "merge"
  for key, mod in MODIFIERS:
    if key in t:
      return mtype, mod
  return mtype, ""


HMIN_RE = re.compile(r"^\s*(\d+):(\d{2})\s*(?:h|hr|hrs)\s*$", re.I)
LABEL_RE = re.compile(r"^([A-Za-z][A-Za-z '&.-]{0,30}),\s+(.*\d.*)$")   # "Work, 85 Inip Drive, Inwood, NY"
SHEET_WORDS = {"resume", "stop", "add a stop", "share drive"}


def split_label(text: str) -> tuple[str, str]:
  """('Work', '85 Inip Drive, Inwood, NY') from a Waze destination line; ('', text) when there is no label."""
  m = LABEL_RE.match(text.strip())
  if m and not any(ch.isdigit() for ch in m.group(1)):
    return m.group(1).strip(), m.group(2).strip()
  return "", text.strip()


def parse(snapshot: dict[str, Any]) -> dict[str, Any]:
  out: dict[str, Any] = {"present": bool(snapshot.get("present", False)), "navigating": False, "distanceM": None,
                         "distanceText": "", "street": "", "then": "", "maneuverType": "", "modifier": "",
                         "etaClock": "", "remainingMin": None, "remainingM": None, "destination": "",
                         "destinationLabel": "", "viaRoad": "", "sheetOpen": False, "via": []}
  nodes = _norm_nodes(snapshot)
  if not out["present"] or not nodes:
    return out
  h = float(snapshot.get("h") or max((n["b"][3] for n in nodes), default=1.0) or 1.0)
  # Waze's instruction banner = the top of the screen (Waze's own window starts at the top, even in split screen)
  banner_bottom = 0.25 * h

  def in_banner(n):
    return n["b"][1] < banner_bottom

  # ---- next maneuver distance: only in the banner (the bottom bar's "20 mi" is the whole trip)
  dists = _distances(nodes)
  top = sorted([d for d in dists if in_banner(d[0])], key=lambda d: d[0]["b"][1])
  id_top = [d for d in top if any(k in d[0]["i"] for k in ID_HINTS["distance"])]
  pick = (id_top or top or [None])[0]
  if pick is not None:
    node, meters, text = pick
    out["distanceM"], out["distanceText"] = round(meters, 1), text
    out["via"].append("distance:id" if id_top else "distance:layout")

  # ---- remaining distance: distance-looking text below the banner (lowest one)
  rest = sorted([d for d in dists if not in_banner(d[0])], key=lambda d: -d[0]["b"][1])
  if rest:
    out["remainingM"] = round(rest[0][1], 1)

  # ---- street: the longest real text in the banner that isn't a distance / time / noise
  cands = []
  for n in nodes:
    t = n["t"]  # real text only: an arrow's content description is not a street
    if not in_banner(n) or not t or (pick is not None and n is pick[0]):
      continue
    if DIST_RE.match(t) or NUM_RE.match(t) or UNIT_RE.match(t) or CLOCK_RE.match(t) or t.lower() in NOISE:
      continue
    if t.lower().startswith(("then", "and then")) or len(t) < 2 or len(t) > 80:
      continue
    cands.append(t)
  if cands:
    out["street"] = max(cands, key=len)
    out["via"].append("street:banner")

  # ---- "then" chip (banner area)
  for n in nodes:
    t = _text(n)
    if n["b"][1] < 0.4 * h and t.lower().startswith(("then", "and then")):
      out["then"] = t
      break

  # ---- maneuver type / direction: only from the banner (Waze draws its arrow as a picture: often unknown)
  for n in (x for x in nodes if in_banner(x)):
    for t in (n["d"], n["t"]):
      if not t:
        continue
      mtype, mod = maneuver_from_text(t)
      if mod:
        out["maneuverType"], out["modifier"] = mtype, mod
        out["via"].append(f"maneuver:{'desc' if t == n['d'] else 'text'}")
        break
    if out["modifier"]:
      break

  # ---- ETA clock and remaining time (lower half)
  for n in sorted(nodes, key=lambda n: -n["b"][1]):
    t = _text(n)
    if n["b"][1] < 0.3 * h:
      continue
    if not out["etaClock"] and CLOCK_RE.match(t):
      out["etaClock"] = t
    if out["remainingMin"] is None and t:
      hm = HMIN_RE.match(t)
      m = MINUTES_RE.match(t)
      if hm:
        out["remainingMin"] = int(hm.group(1)) * 60 + int(hm.group(2))
      elif m and (m.group(1) or m.group(2)):
        out["remainingMin"] = int(m.group(1) or 0) * 60 + int(m.group(2) or 0)

  # ---- expanded bottom sheet ("Work, 85 Inip Drive, Inwood, NY" above "Via Belt Pkwy E Brooklyn")
  texts = sorted([n for n in nodes if n["t"] and not in_banner(n)], key=lambda n: (n["b"][1], n["b"][0]))
  out["sheetOpen"] = any(n["t"].strip().lower() in SHEET_WORDS for n in texts)
  for i, n in enumerate(texts):
    if n["t"].lower().startswith("via ") and i > 0:
      out["viaRoad"] = n["t"][4:].strip()
      prev = texts[i - 1]["t"]
      if not (CLOCK_RE.match(prev) or DIST_RE.match(prev) or MINUTES_RE.match(prev) or HMIN_RE.match(prev)):
        out["destinationLabel"], out["destination"] = split_label(prev)
        out["via"].append("destination:sheet")
      break
  if not out["destination"]:
    for n in texts:
      t = n["t"]
      m = re.match(r"^(?:drive to|driving to|navigating to)\s+(.{2,120})$", t, re.I)
      if m:
        out["destinationLabel"], out["destination"] = split_label(m.group(1))
        out["via"].append("destination:text")
        break

  out["navigating"] = out["distanceM"] is not None and bool(out["street"] or out["then"] or out["etaClock"])
  return out


# ------------------------------------------------------------------ comparison with the Mapbox route
ABBREV = {"st": "street", "ave": "avenue", "av": "avenue", "rd": "road", "blvd": "boulevard", "dr": "drive",
          "ln": "lane", "hwy": "highway", "pkwy": "parkway", "expy": "expressway", "tpke": "turnpike", "ct": "court",
          "pl": "place", "n": "north", "s": "south", "e": "east", "w": "west", "rt": "route", "rte": "route",
          "i": "interstate", "us": "us", "nj": "nj", "ny": "ny"}
STOP = {"the", "onto", "on", "to", "toward", "towards", "via", "turn", "left", "right", "keep", "exit", "take",
        "slight", "sharp", "continue", "straight", "bear", "and", "at", "of", "ramp"}


DIRECTIONS = {"north", "south", "east", "west", "northbound", "southbound", "eastbound", "westbound"}


def street_tokens(text: str) -> set[str]:
  t = re.sub(r"([a-z])-(\d)", r"\1 \2", text.lower())
  words = [ABBREV.get(w, w) for w in re.findall(r"[a-z]+|\d+", t) if w not in STOP]
  core = {w for w in words if w not in DIRECTIONS}
  return core or set(words)          # "878 North" -> {"878"}; a bare "North" stays {"north"}


def street_match(a: str, b: str) -> bool:
  ta, tb = street_tokens(a), street_tokens(b)
  if not ta or not tb:
    return False
  # a shared route number (878, 287, 495 ...) is enough: Waze says "878 North", Mapbox "NY 878; Nassau Expressway"
  if any(w.isdigit() and len(w) >= 2 for w in ta & tb):
    return True
  inter = len(ta & tb)
  return inter / min(len(ta), len(tb)) >= 0.6


ROAD_WORDS = {"st", "street", "ave", "avenue", "av", "rd", "road", "blvd", "boulevard", "pkwy", "parkway", "expy",
              "expressway", "hwy", "highway", "tpke", "turnpike", "dr", "drive", "ln", "lane", "way", "pl", "place",
              "ct", "court", "bridge", "tunnel", "route", "rte", "fwy", "freeway", "thruway", "causeway", "plaza"}


def road_names(text: str) -> list[str]:
  """Waze signposts like 'to Kennedy Airport / Belt Pkwy W / I-678 Van Wyck Expwy' -> the parts that are roads."""
  t = re.sub(r"^\s*(?:to|toward|towards)\s+", "", text or "", flags=re.I)
  parts = [p.strip() for p in re.split(r"\s*[/;|]\s*", t) if p.strip()]
  roads = [p for p in parts if re.search(r"\d", p) or any(w.strip(".,").lower() in ROAD_WORDS for w in p.split())]
  return roads or parts


def compare(waze: dict[str, Any], mapbox_state: dict[str, Any] | None, upcoming: list[tuple[str, float | None]] | None = None) -> dict[str, Any]:
  """Does Waze's next maneuver look like the Mapbox route's next maneuver?"""
  res: dict[str, Any] = {"wazeStreet": waze.get("street", ""), "wazeDistance": waze.get("distanceM"),
                         "wazeModifier": waze.get("modifier", ""), "wazeAgree": None, "wazeWhy": ""}
  if not waze.get("navigating") or not mapbox_state or not mapbox_state.get("valid"):
    res["wazeWhy"] = "no-waze" if not waze.get("navigating") else "no-mapbox"
    return res
  primary = str(mapbox_state.get("maneuverPrimaryText") or "")
  md = float(mapbox_state.get("maneuverDistance") or 0.0)
  res["mbStreet"], res["mbDistance"] = primary, round(md, 1)
  if str(mapbox_state.get("maneuverType") or "") == "depart":
    res["wazeWhy"] = "starting"          # Mapbox's first step is "head <dir> on ...": nothing to compare yet
    return res
  mb_text = f"{primary} {mapbox_state.get('maneuverSecondaryText', '')}"
  s_ok = street_match(waze.get("street", ""), mb_text) if waze.get("street") else False
  wd = waze.get("distanceM")
  d_ok = wd is not None and abs(float(wd) - md) <= max(150.0, 0.25 * md)
  wm, mm = waze.get("modifier", ""), str(mapbox_state.get("maneuverModifier") or "")
  m_ok = bool(wm) and bool(mm) and (wm == mm or wm.lower().endswith(mm.lower()[-4:]))
  # Waze's arrow is a picture, so its direction is usually unknown: then a matching distance alone counts
  res["wazeAgree"] = bool(s_ok or (d_ok and (m_ok or not wm)))
  res["wazeWhy"] = ",".join(k for k, v in (("street", s_ok), ("distance", d_ok), ("direction", m_ok)) if v) or "none"
  if not res["wazeAgree"] and upcoming and waze.get("street") and wd is not None:
    # Mapbox often has small steps Waze skips ("continue onto ...", name changes): Waze's maneuver may be a later one
    for text, dist in upcoming:
      if not text or not street_match(waze["street"], text):
        continue
      if dist is None or abs(float(wd) - float(dist)) <= max(300.0, 0.35 * float(wd)):
        res["wazeAgree"], res["wazeWhy"] = True, "later step"
        break
  return res
