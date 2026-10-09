import json
import time

from openpilot.starpilot.navigation import waze_bridge, waze_parser


def node(t="", d="", i="", b=(0, 0, 0, 0), c="TextView"):
  return {"t": t, "d": d, "i": i, "b": list(b), "c": c}


def nav_snapshot(**kw):
  nodes = [
    node(d="Turn right", c="ImageView", b=(20, 20, 140, 140)),
    node("0.3", b=(150, 30, 230, 90)),
    node("mi", b=(235, 40, 280, 90)),
    node("Main St", b=(150, 95, 600, 140)),
    node("Then", b=(20, 160, 90, 190)),
    node("12:45 PM", b=(40, 1500, 200, 1560)),
    node("18 min", b=(260, 1500, 400, 1560)),
    node("7.4 mi", b=(440, 1500, 600, 1560)),
  ]
  snap = {"v": 1, "t": 1, "w": 1200, "h": 1600, "present": True, "nodes": nodes}
  snap.update(kw)
  return snap


def test_parse_layout_heuristics():
  p = waze_parser.parse(nav_snapshot())
  assert p["navigating"]
  assert abs(p["distanceM"] - 0.3 * 1609.344) < 1
  assert p["street"] == "Main St"
  assert p["modifier"] == "right"
  assert p["etaClock"] == "12:45 PM"
  assert p["remainingMin"] == 18
  assert abs(p["remainingM"] - 7.4 * 1609.344) < 1


def test_parse_ids_and_destination():
  snap = nav_snapshot()
  snap["nodes"].append(node("Drive to ShopRite of Lakewood", b=(40, 1400, 800, 1440)))
  snap["nodes"].append(node("500 ft", i="next_distance", b=(150, 300, 260, 340)))
  p = waze_parser.parse(snap)
  assert p["destination"] == "ShopRite of Lakewood"
  assert abs(p["distanceM"] - 500 * 0.3048) < 1  # id hint beats layout
  assert "distance:id" in p["via"]


def test_not_present_or_not_navigating():
  assert not waze_parser.parse({"present": False, "nodes": []})["navigating"]
  p = waze_parser.parse({"present": True, "h": 1600, "nodes": [node("Where to?", b=(0, 100, 500, 150))]})
  assert not p["navigating"]


def test_street_match_and_compare():
  assert waze_parser.street_match("Main St", "Main Street")
  assert waze_parser.street_match("I-287 N", "Interstate 287 North")
  assert waze_parser.street_match("Exit 9: Garden State Pkwy", "Garden State Parkway")
  assert not waze_parser.street_match("Main St", "Broad Street")
  p = waze_parser.parse(nav_snapshot())
  agree = waze_parser.compare(p, {"valid": True, "maneuverPrimaryText": "Main Street", "maneuverDistance": 480.0, "maneuverModifier": "right"})
  assert agree["wazeAgree"] is True
  dis = waze_parser.compare(p, {"valid": True, "maneuverPrimaryText": "Broad Street", "maneuverDistance": 2500.0, "maneuverModifier": "left"})
  assert dis["wazeAgree"] is False
  assert waze_parser.compare(p, None)["wazeAgree"] is None


class FakeParams:
  def __init__(self, d=None):
    self.d = dict(d or {})

  def get(self, k, encoding=None):
    return self.d.get(k)

  def put(self, k, v):
    self.d[k] = v

  def remove(self, k):
    self.d.pop(k, None)


class Resp:
  status_code = 200

  def json(self):
    return {"features": [{"geometry": {"coordinates": [-74.2, 40.1]}, "properties": {"full_address": "ShopRite, Lakewood NJ"}}]}


class Sess:
  def __init__(self):
    self.calls = []

  def get(self, url, params=None, timeout=None):
    self.calls.append(params)
    return Resp()


class Pos:
  latitude, longitude = 40.0, -74.0


def real_split_screen(sheet=False):
  """Layout seen on the Lenovo tablet (Oct 8): Waze in the right half, banner on top, bottom bar / sheet below."""
  nodes = [
    node("0.3 miles", i="distance_text", b=(1136, 80, 1310, 135)),
    node("Ocean Pkwy", i="street_text", b=(1136, 140, 1400, 185)),
    node(d="", i="direction_icon", c="ImageView", b=(1040, 80, 1110, 160)),
  ]
  if sheet:
    nodes += [node("12:41 PM", b=(1420, 340, 1590, 390)), node("59 min", b=(1400, 395, 1480, 430)),
              node("20 mi", b=(1520, 395, 1600, 430)),
              node("Work, 85 Inip Drive, Inwood, NY", b=(1330, 480, 1700, 510)),
              node("Via Belt Pkwy E Brooklyn", b=(1340, 525, 1670, 560)), node("Add a stop", b=(1340, 730, 1460, 760)),
              node("Share drive", b=(1450, 1095, 1560, 1120)), node("Stop", b=(1050, 1165, 1120, 1200)),
              node("Resume", b=(1520, 1165, 1640, 1200))]
  else:
    nodes += [node("12:34 PM", i="eta_time", b=(1420, 1130, 1590, 1175)), node("1:02 h", i="eta_duration", b=(1400, 1180, 1480, 1215)),
              node("20 mi", i="eta_distance", b=(1520, 1180, 1600, 1215))]
  return {"v": 1, "t": 1, "w": 2000, "h": 1250, "present": True, "nodes": nodes}


def test_real_layout_driving_screen():
  p = waze_parser.parse(real_split_screen())
  assert p["navigating"] and p["distanceText"] == "0.3 miles" and p["street"] == "Ocean Pkwy"
  assert p["remainingMin"] == 62 and abs(p["remainingM"] - 20 * 1609.344) < 1
  assert p["destination"] == ""          # not on the driving screen
  assert p["etaClock"] == "12:34 PM"


def test_real_layout_sheet_open():
  p = waze_parser.parse(real_split_screen(sheet=True))
  assert p["sheetOpen"] and p["navigating"]
  assert p["destinationLabel"] == "Work" and p["destination"] == "85 Inip Drive, Inwood, NY"
  assert p["viaRoad"] == "Belt Pkwy E Brooklyn"
  assert p["street"] == "Ocean Pkwy"


def setup_feed(tmp_path, monkeypatch, params):
  for k, v in (("STATE_PATH", "w.json"), ("OUR_DEST_PATH", "ours.json"), ("OFF_FLAG", "off"), ("LOG_DIR", "logs")):
    monkeypatch.setattr(waze_bridge, k, str(tmp_path / v))
  mem = FakeParams({"LastGPSPosition": json.dumps({"hasFix": True, "latitude": 40.0, "longitude": -74.0})})
  feed = waze_bridge.WazeFeed()
  feed._params, feed._params_mem = params, mem
  feed._session = Sess()
  return feed, mem


WAIT_S = 5.0  # a busy test machine can be slow; a lookup still running after this is a real failure


def wait(feed):
  deadline = time.monotonic() + WAIT_S
  while feed._geocode_inflight:
    assert time.monotonic() < deadline, f"address lookup still running after {WAIT_S:.0f} s"
    time.sleep(0.01)


def test_feed_sets_and_clears_destination(tmp_path, monkeypatch):
  params = FakeParams({"MapboxPublicKey": "pk.x"})
  feed, mem = setup_feed(tmp_path, monkeypatch, params)
  r = feed.ingest(dict(real_split_screen(), record=True))
  assert r["needDestination"] and "NavDestination" not in params.d       # driving screen: no destination yet
  assert list((tmp_path / "logs").glob("waze-*.jsonl"))
  r = feed.ingest(real_split_screen(sheet=True))
  wait(feed)
  dest = json.loads(params.d["NavDestination"])
  assert dest["latitude"] == 40.1 and dest["longitude"] == -74.2 and dest["place_name"].startswith("Work: ")
  assert feed._session.calls[0]["q"] == "85 Inip Drive, Inwood, NY" and feed._session.calls[0]["proximity"] == "-74.0,40.0"
  r = feed.ingest(real_split_screen())                                      # sheet closed again
  assert not r["needDestination"] and r["parsed"]["destination"] == "85 Inip Drive, Inwood, NY"
  assert len(feed._session.calls) == 1
  # Waze ends the route: after END_CLEAR_S our destination is removed
  monkeypatch.setattr(waze_bridge, "END_CLEAR_S", 0.0)
  idle = {"v": 1, "present": True, "h": 1250, "nodes": [node("Where to?", b=(1100, 100, 1500, 150))]}
  feed.ingest(idle)
  feed.ingest(idle)
  assert "NavDestination" not in params.d


def test_feed_keeps_user_destination(tmp_path, monkeypatch):
  user = json.dumps({"place_name": "Work", "latitude": 41.0, "longitude": -73.0})
  params = FakeParams({"NavDestination": user})
  feed, _ = setup_feed(tmp_path, monkeypatch, params)
  monkeypatch.setattr(waze_bridge, "END_CLEAR_S", 0.0)
  idle = {"v": 1, "present": True, "h": 1250, "nodes": [node("Where to?", b=(1100, 100, 1500, 150))]}
  feed.ingest(idle)
  feed.ingest(idle)
  assert params.d["NavDestination"] == user


def test_hybrid_compare(tmp_path, monkeypatch):
  params = FakeParams()
  feed, mem = setup_feed(tmp_path, monkeypatch, params)
  feed.ingest(real_split_screen())
  hy = waze_bridge.WazeHybrid(params, mem)
  hy.update()
  f = hy.state_fields({"valid": True, "maneuverPrimaryText": "Ocean Parkway", "maneuverDistance": 500.0, "maneuverModifier": "right"})
  assert f["wazeFresh"] and f["wazeAgree"] is True


def test_off_flag(tmp_path, monkeypatch):
  params = FakeParams({"MapboxPublicKey": "pk.x"})
  feed, mem = setup_feed(tmp_path, monkeypatch, params)
  (tmp_path / "off").write_text("")
  r = feed.ingest(real_split_screen(sheet=True))
  assert r["off"] and not r["needDestination"] and "NavDestination" not in params.d
  hy = waze_bridge.WazeHybrid(params, mem)
  hy.update()
  assert hy.state_fields({"valid": True}) == {"wazeFresh": False}


class FailSess(Sess):
  def __init__(self):
    super().__init__()
    self.fail = True

  def get(self, url, params=None, timeout=None):
    self.calls.append(params)
    if self.fail:
      msg = "HTTPSConnectionPool(host='api.mapbox.com'): Failed to resolve 'api.mapbox.com' "
      msg += "([Errno -3] Temporary failure in name resolution) access_token=pk.secret"
      raise OSError(msg)
    return Resp()


def test_feed_retries_after_network_error_with_sheet_closed(tmp_path, monkeypatch):
  params = FakeParams({"MapboxPublicKey": "pk.x"})
  feed, _ = setup_feed(tmp_path, monkeypatch, params)
  feed._session = FailSess()
  monkeypatch.setattr(waze_bridge, "NET_RETRY_S", 0.0)
  r = feed.ingest(real_split_screen(sheet=True))
  wait(feed)
  r = feed.ingest(real_split_screen())
  assert "no internet" in r["handoff"] and "pk." not in r["handoff"] and "NavDestination" not in params.d
  feed._session.fail = False
  feed.ingest(real_split_screen())          # sheet closed: retries with the remembered address
  wait(feed)
  assert json.loads(params.d["NavDestination"])["latitude"] == 40.1
  assert feed._session.calls[-1]["q"] == "85 Inip Drive, Inwood, NY"


def test_and_then_row_is_not_the_street():
  snap = real_split_screen()
  snap["nodes"][1]["t"] = "Ave F"
  snap["nodes"].append(node("and then", b=(1020, 220, 1110, 250)))
  p = waze_parser.parse(snap)
  assert p["street"] == "Ave F" and p["then"] == "and then"


def test_highway_names_and_distance_only():
  assert waze_parser.street_match("878 North", "NY 878")
  assert waze_parser.street_match("878 North", "Nassau Expressway NY 878 North")
  assert not waze_parser.street_match("878 North", "I-495 West")
  w = {"navigating": True, "street": "878 North", "distanceM": 152.0, "modifier": ""}
  r = waze_parser.compare(w, {"valid": True, "maneuverPrimaryText": "Nassau Expressway", "maneuverDistance": 180.0, "maneuverModifier": "left"})
  assert r["wazeAgree"] is True and r["wazeWhy"] == "distance" and r["mbStreet"] == "Nassau Expressway"
  r = waze_parser.compare(w, {"valid": True, "maneuverPrimaryText": "Burnside Avenue", "maneuverDistance": 2400.0})
  assert r["wazeAgree"] is False and r["mbDistance"] == 2400.0
  r = waze_parser.compare(w, {"valid": True, "maneuverType": "depart", "maneuverPrimaryText": "x", "maneuverDistance": 50.0})
  assert r["wazeAgree"] is None and r["wazeWhy"] == "starting"


class StreetResp:
  status_code = 200

  def __init__(self, lat, lon):
    self.lat, self.lon = lat, lon

  def json(self):
    return {"features": [{"geometry": {"coordinates": [self.lon, self.lat]}}]}


class StreetSess:
  def __init__(self, lat, lon):
    self.calls, self.lat, self.lon = [], lat, lon

  def get(self, url, params=None, timeout=None):
    self.calls.append(params)
    return StreetResp(self.lat, self.lon)


class P:
  def __init__(self, lat, lon):
    self.latitude, self.longitude = lat, lon


def disagreeing_hybrid(tmp_path, monkeypatch, sess):
  monkeypatch.setattr(waze_bridge, "STATE_PATH", str(tmp_path / "w.json"))
  monkeypatch.setattr(waze_bridge, "OFF_FLAG", str(tmp_path / "off"))
  params = FakeParams({"MapboxPublicKey": "pk.x"})
  feed = waze_bridge.WazeFeed()
  feed._params, feed._params_mem = params, FakeParams()
  feed.ingest(real_split_screen())                      # Waze: Ocean Pkwy in 0.3 mi
  hy = waze_bridge.WazeHybrid(params, FakeParams(), session=sess)
  hy.WAZE_WINS_S = 0.0
  hy.update()
  f = hy.state_fields({"valid": True, "maneuverPrimaryText": "Coney Island Avenue", "maneuverDistance": 1500.0})
  assert f["wazeAgree"] is False
  return hy


def wait_via(hy):
  deadline = time.monotonic() + WAIT_S
  while hy._via_inflight:
    assert time.monotonic() < deadline, f"street lookup still running after {WAIT_S:.0f} s"
    time.sleep(0.01)


def test_waze_wins_reroutes_via_waze_street(tmp_path, monkeypatch):
  # car at 40.63,-73.97 heading north; Waze's turn 0.3 mi (~483 m) ahead -> Ocean Pkwy found ~100 m from there
  near = waze_bridge.project(40.63, -73.97, 0.0, 0.3 * 1609.344)
  sess = StreetSess(near[0] + 0.0009, near[1])
  hy = disagreeing_hybrid(tmp_path, monkeypatch, sess)
  hy.maybe_waze_wins(P(40.63, -73.97), 0.0, 10.0)
  wait_via(hy)
  assert sess.calls[0]["q"] == "Ocean Pkwy" and sess.calls[0]["types"] == "street"
  via = hy.take_via()
  assert via is not None and abs(via[0] - near[0] - 0.0009) < 1e-9
  assert hy.take_via() is None                            # handed out once
  hy.maybe_waze_wins(P(40.63, -73.97), 0.0, 10.0)         # same street again within 60 s: no new lookup
  assert len(sess.calls) == 1
  f = hy.state_fields({"valid": True, "maneuverPrimaryText": "Coney Island Avenue", "maneuverDistance": 1500.0})
  assert "rerouting via Ocean Pkwy" in f["wazeReroute"]


def test_waze_wins_rejects_far_street_and_parked(tmp_path, monkeypatch):
  sess = StreetSess(41.5, -73.0)                          # same-named street far away
  hy = disagreeing_hybrid(tmp_path, monkeypatch, sess)
  hy.maybe_waze_wins(P(40.63, -73.97), 0.0, 0.0)          # parked: nothing
  assert not sess.calls
  hy.maybe_waze_wins(P(40.63, -73.97), 0.0, 10.0)
  wait_via(hy)
  assert hy.take_via() is None and "too far" in hy.reroute_status


def test_route_engine_via_url():
  from openpilot.starpilot.navigation import route_engine as re_
  class S:
    def get(self, url, params=None, timeout=None):
      self.url, self.params = url, params
      class R:
        status_code = 500
        def json(self):
          return {}
      return R()
  s = S()
  eng = re_.MapboxRouteEngine(session=s)
  eng.fetch_route("tok", re_.Coordinate(40.0, -74.0), {"latitude": 41.0, "longitude": -73.0}, 90.0, via=re_.Coordinate(40.5, -73.5))
  assert s.url.endswith("/-74.0,40.0;-73.5,40.5;-73.0,41.0") and s.params["waypoints"] == "0;2" and s.params["bearings"] == "90,90;;"


def test_signpost_and_later_step():
  sign = "to Kennedy Airport / Belt Pkwy W / I-678 Van Wyck Expwy"
  assert waze_parser.road_names(sign) == ["Belt Pkwy W", "I-678 Van Wyck Expwy"]
  w = {"navigating": True, "street": sign, "distanceM": 3862.0, "modifier": ""}
  mb = {"valid": True, "maneuverPrimaryText": "Rockaway Boulevard", "maneuverDistance": 600.0}
  assert waze_parser.compare(w, mb)["wazeAgree"] is False
  up = [("Continue onto Rockaway Boulevard", 600.0), ("Take the ramp toward Belt Parkway West", 3800.0)]
  r = waze_parser.compare(w, mb, up)
  assert r["wazeAgree"] is True and r["wazeWhy"] == "later step"
  assert waze_parser.compare(w, mb, [("Take the ramp toward Belt Parkway West", 9000.0)])["wazeAgree"] is False


def test_timers_ignore_a_clock_correction(tmp_path, monkeypatch):
  """timed can move the wall clock (GPS time) at any moment; retry and freshness are timed on the monotonic clock."""
  params = FakeParams({"MapboxPublicKey": "pk.x"})
  feed, _ = setup_feed(tmp_path, monkeypatch, params)
  feed._session = FailSess()
  monkeypatch.setattr(waze_bridge, "NET_RETRY_S", 0.05)
  feed.ingest(real_split_screen(sheet=True))
  wait(feed)
  assert "NavDestination" not in params.d
  wall = waze_bridge._wall()
  monkeypatch.setattr(waze_bridge, "_wall", lambda: wall - 3600.0)    # clock corrected back an hour
  feed._session.fail = False
  time.sleep(0.06)
  feed.ingest(real_split_screen())                                    # the retry still comes after 0.05 s
  wait(feed)
  assert json.loads(params.d["NavDestination"])["latitude"] == 40.1
  assert waze_bridge.load_state() is not None                        # the state file is still fresh


def test_state_file_age_is_monotonic(tmp_path, monkeypatch):
  params = FakeParams()
  feed, _ = setup_feed(tmp_path, monkeypatch, params)
  feed.ingest(real_split_screen())
  assert waze_bridge.load_state() is not None
  monkeypatch.setattr(waze_bridge, "_wall", lambda: 0.0)              # a wall clock jump changes nothing
  assert waze_bridge.load_state() is not None
  mono = time.monotonic() + waze_bridge.FRESH_S + 1
  monkeypatch.setattr(waze_bridge.time, "monotonic", lambda: mono)
  assert waze_bridge.load_state() is None
