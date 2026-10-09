#!/usr/bin/env python3
"""
starviewd -- StarView bridge daemon (Step 2).

Republishes cereal messages and the livestream H.264 camera feed over WebSocket to a tablet
on the USB tether (or any network), so the tablet can render a full native UI while the comma
does nothing but copy bytes.

Endpoints (default port 8090, env STARVIEW_PORT):
  GET  /                 test page: message/bytes counters per service + live video via WebCodecs
  GET  /status           JSON: clients, per-service rates, daemon CPU%
  WS   /data             text hello (JSON) then binary frames:  u8 name_len | name | raw capnp log.Event
                         client may send text JSON: {"subscribe": {"carState": 20, "modelV2": 0}}  (Hz, 0 = native)
                                                    {"ping": <any>}  -> {"pong": <any>, "t_ns": <device monotonic ns>}
  WS   /term             bash in a pty, persistent per ?sid=
  GET  /hello?n=<nonce>  pairing step 1: proof that this comma holds the tablet's pairing key (see pairing.py)
  POST /auth             pairing step 2: the tablet's proof -> a session pass for every other endpoint
  POST /waze             the tablet's Waze reader: raw Waze screen snapshot -> parsed + compared with the Mapbox
                         route (starpilot/navigation/waze_bridge.py); GET /waze = last result
  HTTP /fs/*             file browser API for the tablet's Files screen (same guard) -- see the files section
  WS   /video/{road|wide|driver}
                         binary frames: 28-byte header | H.264 Annex-B (SPS/PPS prepended on keyframes)
                         header  = struct '<IQQIHH': frameId, timestampEof(ns mono), unixTimestampNanos, flags(bit0=key), width, height
                         The first frame a client receives is always a keyframe.

Design notes:
  * Zero cost when idle: msgq sockets are only created once a client asks for a service, and the reader
    thread sleeps while there are no clients at all.
  * Data path is copy-only: bytes from msgq -> WebSocket. No capnp parsing (except video, to split header/data).
  * Per-client, per-service rate limiting so a 100 Hz service can be delivered at 20 Hz to the UI.
  * Sets the IsLiveStreaming param while a video client is connected so manager starts stream_encoderd
    (process_config's stream_encoderd predicate must allow it while in a car -- see install_starview.py).
"""
import asyncio
import fcntl
import hashlib
import pty
import signal
import termios
import json
import math
import os
import secrets
import string
import struct
import shutil
import subprocess
import threading
import time
from collections import defaultdict
from pathlib import Path

from aiohttp import web

from cereal import messaging
from cereal.services import SERVICE_LIST
from openpilot.common.params import Params, ParamKeyType
from openpilot.common.swaglog import cloudlog
from openpilot.starpilot.system.starview import pairing

try:
  from openpilot.system.version import get_build_metadata
except Exception:  # pragma: no cover
  get_build_metadata = None

PORT = int(os.getenv("STARVIEW_PORT", "8090"))
HOST = os.getenv("STARVIEW_HOST", "0.0.0.0")

# service -> default max rate (Hz) delivered to a client. 0 = native rate.
DEFAULT_RATES = {
  "carState": 20, "controlsState": 20, "selfdriveState": 20, "modelV2": 0, "radarState": 0,
  "longitudinalPlan": 10, "deviceState": 0, "driverMonitoringState": 10, "liveCalibration": 0,
  "navInstruction": 0, "carControl": 10, "pandaStates": 2, "liveTorqueParameters": 0,
  "starpilotPlan": 0, "starpilotSelfdriveState": 20, "starpilotCarState": 20, "starpilotDeviceState": 0,
  "starpilotOnroadEvents": 0, "starpilotLateralState": 20,
}
DEFAULT_RATES = {k: v for k, v in DEFAULT_RATES.items() if k in SERVICE_LIST}

# ---- params the tablet UI needs (read every second, pushed to /data clients as {"type":"params"}) ----
UI_PARAMS_PERSISTENT = [
  "IsMetric", "CameraView", "ModelUI", "AccelerationPath", "RainbowPath", "DynamicPathWidth",
  "PathColor", "LaneLinesColor", "PathEdgesColor", "HideLeadMarker", "LeadInfo", "RadarTracksUI",
  "LeadDetectionThreshold", "AdjacentLeadsUI", "AdjacentPath", "AdjacentPathMetrics", "BlindSpotPath",
  "LaneDetectionWidth", "ShowStoppingPoint", "ShowStoppingPointMetrics", "PedalsOnUI", "StaticPedalsOnUI",
  "DynamicPedalsOnUI", "ShowBrakeStatus", "OnroadDistanceButton", "HideSteeringWheel", "RotatingWheel",
  "ExperimentalMode", "ExperimentalModeConfirmed", "SafeMode", "ConditionalExperimental", "PersistExperimentalState",
  "ShowSpeedLimits", "UseVienna", "ShowSLCOffset", "SpeedLimitSources", "SLCAbbreviatedSources", "SLCActiveSourcesOnly",
  "RoadNameUI", "NavigationUI", "NavDestination", "NavLanePositioningAllowed", "NavDesiresAllowed", "EnableTorqueBarWidget", "ShowSteering", "SignalMetrics",
  "BlindSpotMetrics", "ShowCSCStatus", "BorderWidth", "StoppedTimer", "QOLVisuals", "Compass", "AlwaysOnDM",
  "DeveloperUI", "DeveloperMetrics", "DeveloperSidebar", "LongitudinalPersonality", "DriverCamera",
  "GalaxyDeviceName", "DongleId", "ForceOnroad", "ForceOffroad",
  "PathWidth", "LaneLinesWidth", "RoadEdgesWidth", "PathEdgeWidth", "LastGPSPosition",
  # settings panels (Toggles / Device / Developer / Software / Network) -- mirrors selfdrive/ui/layouts/settings/*.py
  "OpenpilotEnabledToggle", "DisengageOnAccelerator", "IsLdwEnabled", "IsRHD", "IsRHDOverride", "IsRhdDetected",
  "RecordFront", "RecordAudio", "AdbEnabled", "SshEnabled", "JoystickDebugMode", "AlphaLongitudinalEnabled",
  "ShowDebugInfo", "GithubUsername", "GsmRoaming", "GsmMetered", "GsmApn", "AutomaticUpdates", "HardwareSerial",
  "UpdaterCurrentDescription", "UpdaterNewDescription", "UpdaterState", "UpdaterTargetBranch", "UpdaterAvailableBranches",
  "UpdateAvailable", "UpdaterFetchAvailable", "UpdateFailedCount", "LastUpdateTime", "GitBranch", "IsTestedBranch", "PrimeType",
  "UsbGpuCompiled", "UsbGpuActive", "UsbGpuLoading",
  # offroad home (selfdrive/ui/layouts/home.py): version/model header, drive stats, offroad alerts
  "DrivingModelName", "Model", "ApiCache_DriveStats", "StarPilotStats",
  "Offroad_TemperatureTooHigh", "Offroad_ConnectivityNeededPrompt", "Offroad_ConnectivityNeeded", "Offroad_UpdateFailed",
  "Offroad_ChestnutNotDetected", "Offroad_ChestnutOverheated", "Offroad_ChestnutPcieUnavailable", "Offroad_ChestnutUncompiled",
  "Offroad_ChestnutUpdateFailed", "Offroad_ChestnutUsbSlow", "Offroad_IsTakingSnapshot", "Offroad_NeosUpdate",
  "Offroad_UnregisteredHardware", "Offroad_CarUnrecognized", "Offroad_NoFirmware", "Offroad_Recalibration",
  "Offroad_DriverMonitoringUncertain", "Offroad_ExcessiveActuation",
]
UI_PARAMS_MEMORY = ["CEStatus", "NavInstructionState", "NavInstructionCollapsed", "VisionSpeedLimit",
                    "SpeedLimitAccepted", "OnroadDistanceButtonPressed", "SwitchbackModeEnabled"]

# what a tablet may WRITE. persistent keys: display toggles + the few the BigUI buttons touch.
CONTROL_WRITE_PERSISTENT = {
  "ExperimentalMode", "SpeedLimitSources", "ModelUI", "AccelerationPath", "RainbowPath", "DynamicPathWidth",
  "HideLeadMarker", "LeadInfo", "RadarTracksUI", "AdjacentPath", "AdjacentPathMetrics", "BlindSpotPath",
  "ShowStoppingPoint", "PedalsOnUI", "OnroadDistanceButton", "HideSteeringWheel", "RotatingWheel",
  "ShowSpeedLimits", "ShowSLCOffset", "RoadNameUI", "NavigationUI", "EnableTorqueBarWidget", "ShowSteering",
  "SignalMetrics", "BlindSpotMetrics", "ShowCSCStatus", "StoppedTimer", "Compass", "CameraView",
  # Toggles panel
  "OpenpilotEnabledToggle", "SafeMode", "DisengageOnAccelerator", "IsLdwEnabled", "AlwaysOnDM", "IsRHD",
  "RecordFront", "RecordAudio", "IsMetric", "LongitudinalPersonality", "ExperimentalModeConfirmed",
  # Developer panel
  "AdbEnabled", "SshEnabled", "JoystickDebugMode", "AlphaLongitudinalEnabled", "ShowDebugInfo",
  # Software / Network
  "AutomaticUpdates", "UpdaterTargetBranch", "GsmRoaming", "GsmMetered", "GsmApn",
}
CONTROL_WRITE_MEMORY = {"OnroadDistanceButtonPressed", "SpeedLimitAccepted", "NavInstructionCollapsed"}
# toggles that restart openpilot when changed while the car is on (toggles.py needs_restart / developer.py)
RESTART_PARAMS = {"OpenpilotEnabledToggle", "SafeMode", "RecordFront", "RecordAudio", "AlphaLongitudinalEnabled"}
# toggles the on-device UI only allows offroad
OFFROAD_ONLY_PARAMS = {"AdbEnabled", "JoystickDebugMode"}


# ------------------------------------------------------------------------------------------ device ops
# Everything the on-device settings panels can do that Galaxy can't: nmcli Wi-Fi/tethering, Galaxy pairing,
# GitHub SSH keys, calibration resets, reboot/power-off, updater.  All run in the executor (blocking OK).
def _run(cmd, timeout=30, sudo_retry=True):
  r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
  if r.returncode != 0 and sudo_retry and ("privilege" in r.stderr.lower() or "not authorized" in r.stderr.lower()
                                            or "permission denied" in r.stderr.lower()):
    r = subprocess.run(["sudo", "-n", *cmd], capture_output=True, text=True, timeout=timeout)
  return r


def put_typed(p, key, val):
  """Params.put is strictly typed per key -- coerce whatever JSON gave us to the key's declared type."""
  try:
    t = p.get_type(key)
  except Exception:
    t = None
  if t == ParamKeyType.BOOL:
    p.put(key, val.strip().lower() in ("1", "true", "yes", "on") if isinstance(val, str) else bool(val))
  elif t == ParamKeyType.INT:
    p.put(key, int(float(val)))
  elif t == ParamKeyType.FLOAT:
    p.put(key, float(val))
  elif t == ParamKeyType.JSON:
    p.put(key, val if isinstance(val, (dict, list)) else json.loads(val))
  elif t == ParamKeyType.STRING:
    p.put(key, str(val))
  else:
    p.put(key, val if isinstance(val, (bool, int, float, str)) else str(val))


def nmcli(*args, timeout=30):
  return _run(["nmcli", *args], timeout=timeout)


def _nm_split(line, nfields):
  """split an `nmcli -t` line into nfields, honouring the backslash escapes nmcli uses for ':'"""
  out, cur, i = [], [], 0
  while i < len(line):
    ch = line[i]
    if ch == "\\" and i + 1 < len(line):
      cur.append(line[i + 1]); i += 2; continue
    if ch == ":" and len(out) < nfields - 1:
      out.append("".join(cur)); cur = []; i += 1; continue
    cur.append(ch); i += 1
  out.append("".join(cur))
  while len(out) < nfields:
    out.append("")
  return out


class DeviceOps:
  def __init__(self, params, params_mem):
    self.params = params
    self.params_mem = params_mem
    self.galaxy_dir = Path("/data/galaxy")
    self._tether_ssid = None
    self._wifi_dev = None

  # ---- helpers ----
  def dongle_id(self):
    try:
      return self.params.get("DongleId", encoding="utf8") or ""
    except Exception:
      return ""

  def tether_ssid(self):
    if self._tether_ssid is None:
      d = self.dongle_id()
      self._tether_ssid = "weedle" + (("-" + d[:4]) if d else "")
    return self._tether_ssid

  def wifi_dev(self):
    if self._wifi_dev is None:
      r = nmcli("-t", "-f", "DEVICE,TYPE", "dev", "status", timeout=10)
      for line in r.stdout.splitlines():
        dev, typ = _nm_split(line, 2)
        if typ == "wifi":
          self._wifi_dev = dev
          break
      if self._wifi_dev is None:
        self._wifi_dev = "wlan0"
    return self._wifi_dev

  def car_params(self):
    try:
      from openpilot.starpilot.common.car_params_capability import capability_car_params_bytes
      from cereal import car
      b = capability_car_params_bytes(self.params)
      return messaging.log_from_bytes(b, car.CarParams) if b else None
    except Exception:
      return None

  # ---- info block (pushed with the params snapshot) ----
  def info(self):
    out = {}
    try:
      out["has_ssh_keys"] = bool(self.params.get("GithubSshKeys"))
    except Exception:
      out["has_ssh_keys"] = False
    out["galaxy_paired"] = self._galaxy_paired()
    out["galaxy_url"] = self._galaxy_url()
    try:
      pt = int(self.params.get("PrimeType", encoding="utf8") or -2)
    except Exception:
      pt = -2
    out["comma_paired"] = pt > -1
    CP = self.car_params()
    if CP is not None:
      try:
        from openpilot.starpilot.common.lateral_only_experimental import lateral_only_experimental_available
        lat_only = lateral_only_experimental_available(CP)
      except Exception:
        lat_only = False
      has_long = self.params.get_bool("AlphaLongitudinalEnabled") if CP.alphaLongitudinalAvailable else CP.openpilotLongitudinalControl
      out["alpha_long_available"] = bool(CP.alphaLongitudinalAvailable)
      out["has_longitudinal_control"] = bool(has_long)
      out["experimental_available"] = bool(has_long or lat_only)
      out["car"] = CP.carFingerprint
    else:
      out["alpha_long_available"] = False
      out["has_longitudinal_control"] = False
      out["experimental_available"] = False
    out["calib"] = self._calib_text()
    out["lock"] = [k for k in ("OpenpilotEnabledToggle", "ExperimentalMode", "SafeMode", "DisengageOnAccelerator", "IsLdwEnabled",
                               "AlwaysOnDM", "IsRHD", "RecordFront", "RecordAudio", "IsMetric") if self._locked(k)]
    return out

  def _locked(self, k):
    try:
      return bool(self.params.get_bool(k + "Lock"))
    except Exception:
      return False

  def _calib_text(self):
    """same text device.py builds for the Reset Calibration description"""
    from cereal import log
    parts = []
    try:
      b = self.params.get("CalibrationParams")
      if b:
        calib = messaging.log_from_bytes(b, log.Event).liveCalibration
        if calib.calStatus != log.LiveCalibrationData.Status.uncalibrated:
          pitch = math.degrees(calib.rpyCalib[1]); yaw = math.degrees(calib.rpyCalib[2])
          parts.append(f"Your device is pointed {abs(pitch):.1f}° {'down' if pitch > 0 else 'up'} and {abs(yaw):.1f}° {'left' if yaw > 0 else 'right'}.")
    except Exception:
      pass
    try:
      b = self.params.get("LiveDelay")
      lag = messaging.log_from_bytes(b, log.Event).liveDelay.calPerc if b else 0
      parts.append(f"Steering lag calibration is {lag}% complete." if lag < 100 else "Steering lag calibration is complete.")
    except Exception:
      pass
    try:
      b = self.params.get("LiveTorqueParameters")
      if b:
        t = messaging.log_from_bytes(b, log.Event).liveTorqueParameters
        if t.useParams:
          parts.append(f"Steering torque response calibration is {t.calPerc}% complete." if t.calPerc < 100 else "Steering torque response calibration is complete.")
    except Exception:
      pass
    return " ".join(parts)

  # ---- Galaxy pairing (device.py) ----
  def _galaxy_paired(self):
    try:
      return len((self.galaxy_dir / "glxyauth").read_text().strip()) == 64
    except Exception:
      return False

  def _galaxy_url(self):
    try:
      slug = (self.galaxy_dir / "glxyslug").read_text().strip()
      return f"https://galaxy.firestar.link/{slug}" if slug else ""
    except Exception:
      return ""

  def galaxy_pair(self, password):
    if len(password or "") < 6:
      return {"ok": False, "error": "Password must be at least 6 characters."}
    self.galaxy_dir.mkdir(parents=True, exist_ok=True)
    (self.galaxy_dir / "glxyauth").write_text(hashlib.sha256(password.encode("utf-8")).hexdigest())
    (self.galaxy_dir / "glxysession").write_text(secrets.token_hex(32))
    slug = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(16))
    (self.galaxy_dir / "glxyslug").write_text(slug)
    return {"ok": True, "url": self._galaxy_url()}

  def galaxy_unpair(self):
    for n in ("glxyauth", "glxysession", "glxyslug"):
      try:
        (self.galaxy_dir / n).unlink()
      except FileNotFoundError:
        pass
    return {"ok": True}

  def comma_pair_url(self):
    from openpilot.common.api import Api
    d = self.dongle_id()
    if not d:
      return {"ok": False, "error": "no DongleId"}
    token = Api(d).get_token({"pair": True})
    return {"ok": True, "url": f"https://connect.comma.ai/?pair={token}"}

  # ---- SSH keys (widgets/ssh_key.py) ----
  def ssh_add(self, username):
    import requests
    username = (username or "").strip()
    if not username:
      return {"ok": False, "error": "empty username"}
    try:
      r = requests.get(f"https://github.com/{username}.keys", timeout=15)
      r.raise_for_status()
      keys = r.text.strip()
      if not keys:
        raise ValueError("no keys")
    except requests.exceptions.Timeout:
      return {"ok": False, "error": "Request timed out"}
    except Exception:
      return {"ok": False, "error": f"No SSH keys found for user '{username}'"}
    self.params.put("GithubUsername", username)
    self.params.put("GithubSshKeys", keys)
    return {"ok": True, "username": username, "keys": len(keys.splitlines())}

  def ssh_remove(self):
    self.params.remove("GithubUsername")
    self.params.remove("GithubSshKeys")
    return {"ok": True}

  # ---- Wi-Fi / tethering (system/ui/widgets/network.py via nmcli) ----
  def wifi_status(self):
    dev = self.wifi_dev()
    ssid, state = "", ""
    r = nmcli("-t", "-f", "DEVICE,STATE,CONNECTION", "dev", "status", timeout=10)
    for line in r.stdout.splitlines():
      d, st, con = _nm_split(line, 3)
      if d == dev:
        state, ssid = st, con
    ip = ""
    r = nmcli("-t", "-g", "IP4.ADDRESS", "dev", "show", dev, timeout=10)
    if r.stdout.strip():
      ip = r.stdout.strip().split("|")[0].split("/")[0]
    metered = "unknown"
    if ssid:
      r = nmcli("-t", "-g", "connection.metered", "con", "show", "id", ssid, timeout=10)
      metered = r.stdout.strip() or "unknown"
    teth = self.tether_ssid()
    return {"ok": True, "ssid": ssid if ssid != teth else "", "state": state, "ip": ip, "tethering": ssid == teth,
            "tether_ssid": teth, "metered": metered, "connecting": state.startswith("connecting")}

  def wifi_scan(self, rescan=False):
    saved = set()
    r = nmcli("-t", "-f", "NAME,TYPE", "con", "show", timeout=10)
    for line in r.stdout.splitlines():
      name, typ = _nm_split(line, 2)
      if typ == "802-11-wireless":
        saved.add(name)
    args = ["-t", "-f", "IN-USE,SIGNAL,SECURITY,SSID", "dev", "wifi", "list", "--rescan", "yes" if rescan else "auto"]
    r = nmcli(*args, timeout=25)
    nets = {}
    for line in r.stdout.splitlines():
      inuse, sig, sec, ssid = _nm_split(line, 4)
      if not ssid or ssid == self.tether_ssid():
        continue
      try:
        sig = int(sig)
      except ValueError:
        sig = 0
      n = nets.get(ssid)
      if n is None or sig > n["strength"]:
        nets[ssid] = {"ssid": ssid, "strength": sig, "secured": bool(sec.strip()), "security": sec.strip(),
                      "connected": inuse.strip() == "*" or (n["connected"] if n else False), "saved": ssid in saved}
      elif inuse.strip() == "*":
        n["connected"] = True
    lst = sorted(nets.values(), key=lambda x: (not x["connected"], -x["strength"]))
    st = self.wifi_status()
    return {"ok": True, "networks": lst, **{k: v for k, v in st.items() if k != "ok"}}

  def wifi_connect(self, ssid, password="", hidden=False):
    if not ssid:
      return {"ok": False, "error": "no ssid"}
    dev = self.wifi_dev()
    if not password:
      # saved profile (or open network)
      r = nmcli("con", "up", "id", ssid, timeout=45)
      if r.returncode == 0:
        return {"ok": True, "ssid": ssid}
      if "Secrets were required" in r.stderr or "no secrets" in r.stderr.lower() or "802-11-wireless-security" in r.stderr:
        return {"ok": False, "need_auth": True, "ssid": ssid, "error": "password required"}
      if "unknown connection" not in r.stderr.lower() and "not found" not in r.stderr.lower() and "no connection" not in r.stderr.lower():
        pass  # fall through and try a fresh connect (e.g. open network with no saved profile)
    if password:
      nmcli("con", "delete", "id", ssid, timeout=10)  # replace a stale profile instead of creating "<ssid> 1"
    args = ["dev", "wifi", "connect", ssid, "ifname", dev]
    if password:
      args += ["password", password]
    if hidden:
      args += ["hidden", "yes"]
    r = nmcli(*args, timeout=60)
    if r.returncode != 0:
      err = (r.stderr or r.stdout).strip()
      need = "Secrets were required" in err or "password" in err.lower() or "802-11-wireless-security" in err
      # nmcli leaves a broken profile behind on a wrong password -- drop it so the next try is clean
      if need:
        nmcli("con", "delete", "id", ssid, timeout=10)
      return {"ok": False, "need_auth": need, "ssid": ssid, "error": err[:300]}
    return {"ok": True, "ssid": ssid}

  def wifi_forget(self, ssid):
    r = nmcli("con", "delete", "id", ssid, timeout=15)
    return {"ok": r.returncode == 0, "ssid": ssid, "error": r.stderr.strip()[:200]}

  def wifi_metered(self, mode):  # "unknown" | "yes" | "no"
    st = self.wifi_status()
    if not st["ssid"]:
      return {"ok": False, "error": "not connected to Wi-Fi"}
    r = nmcli("con", "modify", "id", st["ssid"], "connection.metered", mode, timeout=15)
    return {"ok": r.returncode == 0, "error": r.stderr.strip()[:200]}

  def _ensure_tether_profile(self):
    r = nmcli("-t", "-f", "NAME", "con", "show", timeout=10)
    if self.tether_ssid() in [l.strip() for l in r.stdout.splitlines()]:
      return
    nmcli("con", "add", "type", "wifi", "ifname", self.wifi_dev(), "con-name", self.tether_ssid(), "autoconnect", "no",
          "ssid", self.tether_ssid(), "802-11-wireless.mode", "ap", "802-11-wireless.band", "bg",
          "ipv4.method", "shared", "ipv4.addresses", "192.168.43.1/24", "ipv4.gateway", "192.168.43.1",
          "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", "swagswagcomma", timeout=20)

  def tether(self, on):
    self._ensure_tether_profile()
    if on:
      r = nmcli("con", "up", "id", self.tether_ssid(), timeout=45)
      if r.returncode == 0:
        try:
          from openpilot.system.ui.lib.tethering_nat import ensure_tethering_nat
          ensure_tethering_nat()
        except Exception as e:
          cloudlog.warning(f"starview: tethering NAT: {e}")
    else:
      r = nmcli("con", "down", "id", self.tether_ssid(), timeout=30)
    return {"ok": r.returncode == 0, "tethering": on, "error": r.stderr.strip()[:200]}

  def tether_password(self, password=None):
    self._ensure_tether_profile()
    if password is None:
      r = nmcli("-s", "-t", "-g", "802-11-wireless-security.psk", "con", "show", "id", self.tether_ssid(), timeout=10)
      return {"ok": True, "password": r.stdout.strip(), "ssid": self.tether_ssid()}
    if len(password) < 8:
      return {"ok": False, "error": "password must be at least 8 characters"}
    r = nmcli("con", "modify", "id", self.tether_ssid(), "wifi-sec.psk", password, timeout=15)
    if r.returncode == 0 and self.wifi_status()["tethering"]:
      nmcli("con", "up", "id", self.tether_ssid(), timeout=45)
    return {"ok": r.returncode == 0, "error": r.stderr.strip()[:200]}

  def gsm_apply(self):
    """push GsmRoaming/GsmApn/GsmMetered params into the 'lte' profile (temporary, like WifiManager.update_gsm_settings)"""
    roaming = self.params.get_bool("GsmRoaming")
    apn = (self.params.get("GsmApn", encoding="utf8") or "").strip()
    metered = self.params.get_bool("GsmMetered")
    args = ["con", "modify", "--temporary", "id", "lte", "gsm.home-only", "no" if roaming else "yes",
            "gsm.apn", apn, "gsm.auto-config", "yes" if apn == "" else "no", "connection.metered", "unknown" if metered else "no"]
    r = nmcli(*args, timeout=15)
    if r.returncode == 0:
      nmcli("con", "up", "id", "lte", timeout=30)
    return {"ok": r.returncode == 0, "error": r.stderr.strip()[:200]}

  # ---- updater (software.py) ----
  def update_check(self):
    os.system("pkill -SIGUSR1 -f system.updated.updated")
    return {"ok": True}

  def update_download(self):
    self.params_mem.put_bool("ManualUpdateInitiated", True)
    os.system("pkill -SIGHUP -f system.updated.updated")
    return {"ok": True}

  def error_log(self):
    try:
      return {"ok": True, "text": Path("/data/error_logs/error.txt").read_text(encoding="utf-8", errors="replace")[-20000:]}
    except Exception:
      return {"ok": True, "text": "No error log found."}

LIVE_HOLD_S = 600.0   # livestream encoder stays on this long after the last video client leaves
VIDEO_CAMS = {"road": "livestreamRoadEncodeData", "wide": "livestreamWideRoadEncodeData", "driver": "livestreamDriverEncodeData"}
V4L2_BUF_FLAG_KEYFRAME = 0x8
VIDEO_HDR = struct.Struct("<IQQIHH")
DATA_QUEUE = 256      # frames per data client before dropping
VIDEO_QUEUE = 8       # frames per video client before resync-on-keyframe
TICK_S = 0.010        # reader thread period (adds <= 10 ms latency)


class DataClient:
  def __init__(self, ws, loop):
    self.ws = ws
    self.loop = loop
    self.q: asyncio.Queue = asyncio.Queue(maxsize=DATA_QUEUE)
    self.rates: dict[str, float] = dict(DEFAULT_RATES)
    self.last_sent: dict[str, float] = defaultdict(float)
    self.dropped = 0
    self.sent = 0
    self.peer = ws._req.remote if hasattr(ws, "_req") else "?"
    self.force_params = False

  def push(self, frame: bytes):  # called on the event loop
    try:
      self.q.put_nowait(frame)
    except asyncio.QueueFull:
      self.dropped += 1


class VideoClient:
  def __init__(self, ws, cam):
    self.ws = ws
    self.cam = cam
    self.q: asyncio.Queue = asyncio.Queue(maxsize=VIDEO_QUEUE)
    self.need_key = True
    self.dropped = 0
    self.sent = 0

  def push(self, frame: bytes, keyframe: bool):
    if self.need_key and not keyframe:
      return
    if self.q.full():
      # fell behind: flush and resync on the next keyframe
      while not self.q.empty():
        self.q.get_nowait()
      self.dropped += 1
      self.need_key = True
      if not keyframe:
        return
    self.need_key = False
    self.q.put_nowait(frame)


class Hub:
  """Owns msgq sockets + the reader thread. Fan-out happens on the asyncio loop via call_soon_threadsafe."""

  def __init__(self, loop):
    self.loop = loop
    self.params = Params()
    self.params_mem = Params(memory=True)
    self.last_params_json = None
    self.lock = threading.Lock()
    self.socks: dict[str, messaging.SubSocket] = {}
    self.sock_conflate: dict[str, bool] = {}
    self.data_clients: set[DataClient] = set()
    self.video_clients: dict[str, set[VideoClient]] = {c: set() for c in VIDEO_CAMS}
    self.stats = defaultdict(lambda: {"msgs": 0, "bytes": 0})
    self.rate_win = defaultdict(lambda: [time.monotonic(), 0, 0])  # t0, msgs, bytes
    self.rates_out: dict[str, tuple[float, float]] = {}
    self.t_start = time.monotonic()
    self.cpu_mark = (time.monotonic(), time.process_time())
    self.cpu_pct = 0.0
    self.wanted_services: set[str] = set()
    self.live = False
    self.drive_hold = False   # tablet attached at the start of a drive: video encoder kept on for the whole drive
    self._vh = None           # video health sockets (camera frames vs recording encoder output)
    self._cam_t = 0.0; self._enc_t = 0.0; self._enc_ok_since = None; self._stall_since = None
    self.enc_stall_s = 0.0    # > 0: cameras deliver frames but the recording encoder has put out nothing for this long
    self.live_off_at = None   # monotonic time the livestream encoder may be switched off (hold after the last video client)
    self.ops = DeviceOps(self.params, self.params_mem)
    self.tablet_mode = True   # tablet asks to turn the comma screen off while connected (control action "tablet_mode")
    self._flag_t = 0.0
    self.engaged = False
    self.started = False
    # StarView runs as its own service (service.py) and outlives openpilot: deviceState (2 Hz) tells whether it's up
    self.openpilot_down = False
    self._ds_sock = None
    self._ds_t = time.monotonic()
    self._info = {}
    self._info_t = 0.0
    self._car_state_sock = None
    # one params snapshot per second shared by every connected tablet (~150 param reads each)
    self._snap_lock = threading.Lock()
    self._snap = None
    self._snap_t = 0.0
    self.thread = threading.Thread(target=self._reader, name="starview-reader", daemon=True)
    self.thread.start()

  # ---- socket management (call with lock) ----
  def _ensure_sockets(self):
    want = set(self.wanted_services)
    for cam, svc in VIDEO_CAMS.items():
      if self.video_clients[cam]:
        want.add(svc)
    # conflate (newest-only reads) whenever every client takes this service rate-limited; native-rate
    # subscribers (rate 0) and video need every message, so those sockets stay non-conflated.
    conflate_ok = {}
    for name in want:
      native = name in VIDEO_CAMS.values() or any(c.rates.get(name) == 0 for c in self.data_clients)
      conflate_ok[name] = not native
    for name in want:
      if name in SERVICE_LIST and (name not in self.socks or self.sock_conflate.get(name) != conflate_ok[name]):
        self.socks[name] = messaging.sub_sock(name, conflate=conflate_ok[name])
        self.sock_conflate[name] = conflate_ok[name]
    for name in list(self.socks):
      if name not in want:
        del self.socks[name]
        self.sock_conflate.pop(name, None)


  def recompute_wanted(self):
    with self.lock:
      w = set()
      for c in self.data_clients:
        w.update(k for k, v in c.rates.items() if v is not None and v >= 0)
      self.wanted_services = {s for s in w if s in SERVICE_LIST}
      self._ensure_sockets()
      want_live = any(self.video_clients[c] for c in VIDEO_CAMS) or self.drive_hold
    # Only while the car is on. IsLiveStreaming also keeps camerad running (process_config: camera_run OR livestream):
    # held on for 10 min after parking, a restart inside that window found the cameras half-running -> "Camera
    # Malfunction" (09-25). Parked, the tablet shows the offroad home anyway: switch off at once, no hold.
    onroad = self._onroad()
    if not onroad:
      want_live = False
      self.live_off_at = time.monotonic()
    # Keep the stream encoder running once started: every start of stream_encoderd is a CPU burst that made
    # calibrationd miss its input deadlines -> "Communication Issue" take-overs (158 encoder starts on 09-22/23, each
    # matching a commIssue burst). The app opens/closes video sockets all the time (Scene/Camera, detector, app
    # switching), so it only goes off after LIVE_HOLD_S with no video client at all.
    if want_live:
      self.live_off_at = None
    elif self.live:
      if self.live_off_at is None:
        self.live_off_at = time.monotonic() + LIVE_HOLD_S
      if time.monotonic() < self.live_off_at:
        return
    if want_live != self.live:
      self.live = want_live
      try:
        self.params.put_bool("IsLiveStreaming", want_live)
        if want_live:
          self.params.put_bool("LivestreamRequestKeyframe", True)
      except Exception as e:
        cloudlog.warning(f"starview: params write failed: {e}")
      cloudlog.info(f"starview: IsLiveStreaming={want_live}")

  def _onroad(self):
    if self.openpilot_down:  # IsOnroad keeps whatever openpilot last wrote when it was stopped
      return False
    try:
      return self.params.get("IsOnroad") in (b"1", "1", True, 1)
    except Exception:
      return False

  def _moving(self) -> bool:
    """Car speed from carState (control actions only). No carState within 0.25 s means the car isn't running."""
    if not self._onroad() and not self.started:
      return False
    try:
      if self._car_state_sock is None:
        self._car_state_sock = messaging.sub_sock("carState", conflate=True, timeout=250)
      msg = messaging.recv_one(self._car_state_sock)
      return msg is not None and msg.carState.vEgo > 0.5
    except Exception as e:
      cloudlog.warning(f"starview: carState read failed: {e}")
      return True  # can't tell: refuse rather than risk switching while moving

  def video_health_tick(self):
    """Cameras deliver frames but the encoders put out nothing: on 09-24 both encoders sat silent for minutes (drive
    recording stopped too, the tablet got no video) and nothing said so. Watch the recording encoder's small index
    messages against the road camera's frames and report a stall (params 'i' block -> tablet banner, /status, log)."""
    now = time.monotonic()
    if self._vh is None:
      # the recording encoder's own output: *EncodeIdx is only written into the log, never broadcast (09-24 false alarm)
      self._vh = {n: messaging.sub_sock(n, conflate=True) for n in ("roadCameraState", "roadEncodeData") if n in SERVICE_LIST}
    for n, sock in self._vh.items():
      try:
        if sock.receive(non_blocking=True) is not None:
          if n == "roadCameraState": self._cam_t = now
          else: self._enc_t = now
      except Exception:
        pass
    try:
      onroad = self.params.get("IsOnroad") in (b"1", "1", True, 1)
    except Exception:
      onroad = False
    cam_ok = now - self._cam_t < 2.0; enc_ok = now - self._enc_t < 3.0
    if onroad and enc_ok:
      if self._enc_ok_since is None: self._enc_ok_since = now
    else:
      self._enc_ok_since = None
    if onroad and cam_ok and not enc_ok:
      if self._stall_since is None: self._stall_since = now
      stall = now - self._stall_since
      if stall >= 10.0 and self.enc_stall_s < 10.0:
        cloudlog.error(f"starview: VIDEO ENCODER STALLED - camera frames arrive but no encoded video for {stall:.0f} s (recording is off)")
      self.enc_stall_s = stall if stall >= 10.0 else 0.0
    else:
      if self.enc_stall_s >= 10.0: cloudlog.warning("starview: video encoder output back")
      self._stall_since = None; self.enc_stall_s = 0.0

  OPENPILOT_DOWN_S = 5.0

  def openpilot_tick(self):
    """Notice openpilot being stopped (`sudo systemctl stop comma`) or started again; StarView keeps running either way.
    Stopped: forget the car state it left behind, so reboot works and the comma screen isn't held dark. Started again:
    the manager clears IsLiveStreaming on start, so ask for the live video again if a tablet is watching."""
    if self._ds_sock is None:
      self._ds_sock = messaging.sub_sock("deviceState", conflate=True)
    now = time.monotonic()
    try:
      if self._ds_sock.receive(non_blocking=True) is not None:
        self._ds_t = now
    except Exception:
      pass
    down = now - self._ds_t > self.OPENPILOT_DOWN_S
    if down != self.openpilot_down:
      self.openpilot_down = down
      cloudlog.info(f"starview: openpilot {'stopped' if down else 'running again'}")
      if down:
        self.started = False
        self.engaged = False
      self.invalidate_params_snapshot()
    if not down and self.live and not self.params.get_bool("IsLiveStreaming"):
      cloudlog.info("starview: openpilot restarted - asking for the live stream again")
      self.live = False
      self.recompute_wanted()

  def drive_hold_tick(self):
    """Warm start: with the tablet attached, switch the video encoder on as the car turns on (still parked) and keep it
    on until the car is off. Its start-up burst (3 camera encoders initialising) came 3 s before a Communication Issue
    on 09-24 when the camera view was first opened mid-drive. Off switch: touch /data/starview/no_drive_hold"""
    if os.path.exists("/data/starview/no_drive_hold"):
      hold = False
    else:
      try:
        onroad = self.params.get("IsOnroad") in (b"1", "1", True, 1)
      except Exception:
        onroad = False
      with self.lock:
        tablet = bool(self.data_clients)
      # never start the stream encoder together with the recording encoder at ignition (09-24: both went silent):
      # only once recording has put out video cleanly for 30 s; once on, a tablet blip mid-drive doesn't switch it off
      settled = self._enc_ok_since is not None and time.monotonic() - self._enc_ok_since >= 30.0
      hold = onroad and self.enc_stall_s == 0.0 and (self.drive_hold or (tablet and settled))
    onroad_now = self._onroad()
    if onroad_now != getattr(self, "_was_onroad", None):
      self._was_onroad = onroad_now
      if not onroad_now and self.live:
        cloudlog.info("starview: car off - live stream (and the cameras it keeps running) off now")
        self.drive_hold = False
        self.recompute_wanted()
    if hold != self.drive_hold:
      self.drive_hold = hold
      cloudlog.info(f"starview: drive hold {'ON' if hold else 'OFF'}")
      self.recompute_wanted()

  def request_keyframe(self):
    try:
      self.params.put_bool_nonblocking("LivestreamRequestKeyframe", True)
    except Exception:
      pass

  # ---- reader thread ----
  def _reader(self):
    while True:
      with self.lock:
        socks = dict(self.socks)
      if not socks:
        time.sleep(0.2)
        now = time.monotonic()
        self.tablet_flag_tick(now)
        if self.live and self.live_off_at is not None and now >= self.live_off_at:
          self.recompute_wanted()
        continue
      time.sleep(TICK_S)  # fixed tick: far cheaper than waking on every one of the 100 Hz messages
      now = time.monotonic()
      for name, sock in socks.items():
        try:
          if self.sock_conflate.get(name):
            m = sock.receive(non_blocking=True)
            msgs = [m] if m is not None else []
          else:
            msgs = messaging.drain_sock_raw(sock)
        except Exception as e:
          cloudlog.exception(f"starview: recv {name}: {e}")
          continue
        if not msgs:
          continue
        st = self.stats[name]
        st["msgs"] += len(msgs)
        st["bytes"] += sum(len(m) for m in msgs)
        rw = self.rate_win[name]
        rw[1] += len(msgs)
        rw[2] += sum(len(m) for m in msgs)
        if now - rw[0] >= 2.0:
          self.rates_out[name] = (rw[1] / (now - rw[0]), rw[2] / (now - rw[0]))
          rw[0], rw[1], rw[2] = now, 0, 0
        if name in VIDEO_CAMS.values():
          self._fanout_video(name, msgs)
        else:
          self._fanout_data(name, msgs, now)
      self.tablet_flag_tick(now)
      if self.live and self.live_off_at is not None and now >= self.live_off_at:
        self.recompute_wanted()
      # cpu accounting
      if now - self.cpu_mark[0] >= 5.0:
        wall = now - self.cpu_mark[0]
        cpu = time.process_time() - self.cpu_mark[1]
        self.cpu_pct = 100.0 * cpu / wall  # percent of ONE core
        self.cpu_mark = (now, time.process_time())

  TABLET_FLAG = "/dev/shm/starview_tablet"

  def tablet_flag_tick(self, now):
    """heartbeat for the comma UI's tablet mode: file mtime refreshed every second while a tablet shows the road view"""
    if now - self._flag_t < 1.0:
      return
    self._flag_t = now
    with self.lock:
      # a connected tablet counts, even while its road view is hidden behind Settings/Terminal/Files
      connected = bool(self.data_clients)
    if connected:
      self._tablet_seen = now
    # Parked: hold 30 s through app switches / reconnects (every drop used to light the comma screen up again).
    # Driving: no hold, so the comma screen comes back within ~5 s (the UI's staleness limit) if the tablet dies.
    hold_s = 0.0 if (self.started or self._onroad()) else 30.0
    on = self.tablet_mode and (connected or now - getattr(self, "_tablet_seen", -1e9) < hold_s)
    if on != getattr(self, "_flag_logged", None):
      self._flag_logged = on
      cloudlog.info(f"starview: tablet flag {'ON' if on else 'OFF'} (clients {len(self.data_clients)}, mode {self.tablet_mode})")
    try:
      if on:
        with open(self.TABLET_FLAG, "w") as f:
          f.write(str(time.time()))
      elif os.path.exists(self.TABLET_FLAG):
        os.unlink(self.TABLET_FLAG)
    except OSError:
      pass

  def _fanout_data(self, name, msgs, now):
    with self.lock:
      clients = list(self.data_clients)
    if not clients:
      return
    prefix = bytes([len(name)]) + name.encode()
    if name == "selfdriveState" or name == "deviceState":
      try:  # tiny messages: parse the newest one so the settings actions can honour engaged/onroad guards
        evt = messaging.log_from_bytes(msgs[-1])
        if name == "selfdriveState":
          self.engaged = bool(evt.selfdriveState.enabled)
        else:
          self.started = bool(evt.deviceState.started)
      except Exception:
        pass
    # rate limiting: deliver at most `rate` Hz per client; always deliver the newest of a burst
    for c in clients:
      rate = c.rates.get(name)
      if rate is None:
        continue
      if rate <= 0:
        for m in msgs:
          self.loop.call_soon_threadsafe(c.push, prefix + m)
        c.sent += len(msgs)
      else:
        # schedule-based limiter: deliver when the next slot is due, then advance the slot by one period
        # (never more than one period behind `now`, so a stall doesn't cause a burst afterwards)
        period = 1.0 / rate
        due = c.last_sent[name]
        if now >= due:
          c.last_sent[name] = due + period if due + period > now else now
          self.loop.call_soon_threadsafe(c.push, prefix + msgs[-1])
          c.sent += 1

  def _fanout_video(self, name, msgs):
    cam = next(k for k, v in VIDEO_CAMS.items() if v == name)
    with self.lock:
      clients = list(self.video_clients[cam])
    if not clients:
      return
    for m in msgs:
      try:
        evt = messaging.log_from_bytes(m)
        ed = getattr(evt, evt.which())
        idx = ed.idx
        key = bool(idx.flags & V4L2_BUF_FLAG_KEYFRAME)
        hdr = VIDEO_HDR.pack(idx.frameId & 0xffffffff, idx.timestampEof, ed.unixTimestampNanos,
                             1 if key else 0, ed.width, ed.height)
        payload = hdr + (ed.header + ed.data if key else ed.data)
      except Exception as e:
        cloudlog.exception(f"starview: video parse: {e}")
        continue
      for c in clients:
        self.loop.call_soon_threadsafe(c.push, payload, key)


  # ---- params snapshot + control ----
  def _read_param(self, p, key):
    try:
      v = p.get(key)
    except Exception:
      return None
    if v is None:
      return None
    if isinstance(v, bytes):
      try:
        v = v.decode("utf-8")
      except Exception:
        return None
    return v

  def params_snapshot(self):
    """Every data client asks once a second; build it at most once per ~second for all of them."""
    with self._snap_lock:
      now = time.monotonic()
      if self._snap is None or now - self._snap_t >= 0.9:
        self._snap = self._build_params_snapshot()
        self._snap_t = now
      return self._snap

  def ops_info_dirty(self):
    self._info_t = 0.0
    self.invalidate_params_snapshot()

  def invalidate_params_snapshot(self):
    with self._snap_lock:
      self._snap = None  # an action changed something: the next snapshot is read fresh

  def _build_params_snapshot(self):
    out = {"type": "params", "p": {}, "m": {}}
    for k in UI_PARAMS_PERSISTENT:
      v = self._read_param(self.params, k)
      if v is not None:
        out["p"][k] = v
    for k in UI_PARAMS_MEMORY:
      v = self._read_param(self.params_mem, k)
      if v is not None:
        out["m"][k] = v
    now = time.monotonic()
    if now - self._info_t > 5.0:
      self._info_t = now
      try:
        self._info = self.ops.info()
      except Exception as e:
        cloudlog.warning(f"starview: info: {e}")
    out["i"] = dict(self._info, engaged=self.engaged, onroad=self.started, encStall=round(self.enc_stall_s),
                    openpilot=not self.openpilot_down)
    return out

  def control(self, req: dict) -> dict:
    """Allow-listed writes. Returns a JSON-able ack."""
    act = req.get("action")
    try:
      if act == "param":
        key, val, mem = req.get("key"), req.get("value"), bool(req.get("memory", False))
        allowed = CONTROL_WRITE_MEMORY if mem else CONTROL_WRITE_PERSISTENT
        if key not in allowed:
          return {"ok": False, "error": f"{key} not writable"}
        p = self.params_mem if mem else self.params
        if not mem:
          if key in RESTART_PARAMS and self.engaged:
            return {"ok": False, "error": f"disengage to change {key}"}
          if key in OFFROAD_ONLY_PARAMS and self.started:
            return {"ok": False, "error": f"{key} can only be changed while offroad"}
          if self.ops._locked(key):
            return {"ok": False, "error": f"{key} is locked"}
        if val is None or val == "":
          p.remove(key)
        else:
          put_typed(p, key, val)
        # side effects the on-device panels apply (toggles.py / developer.py / network.py)
        if not mem:
          if key == "IsRHD":
            self.params.put_bool("IsRHDOverride", True)
          if key == "JoystickDebugMode":
            self.params.put_bool("LongitudinalManeuverMode", False)
          if key == "SafeMode" and val:
            self.params.put_bool("ExperimentalMode", False)
            self.params.put_int("LongitudinalPersonality", 2)  # relaxed
          if key == "ExperimentalMode" and val:
            self.params.put_bool("ExperimentalModeConfirmed", True)
          if key in RESTART_PARAMS:
            self.params.put_bool("OnroadCycleRequested", True)
          if key in ("GsmRoaming", "GsmMetered", "GsmApn"):
            self.ops.gsm_apply()
          if key == "UpdaterTargetBranch":
            self.ops.update_check()
        return {"ok": True, "key": key, "value": val}
      # ---- Device panel ----
      if act == "reset_calibration":
        if self.engaged:
          return {"ok": False, "error": "Disengage to Reset Calibration"}
        for k in ("CalibrationParams", "LiveTorqueParameters", "LiveParameters", "LiveParametersV2", "LiveDelay"):
          self.params.remove(k)
        self.params.put_bool("OnroadCycleRequested", True)
        self.ops_info_dirty()
        return {"ok": True}
      if act == "reset_dm":
        if self.engaged:
          return {"ok": False, "error": "Disengage to Reset Driver Monitoring"}
        for k in ("IsRhdDetected", "IsRHD", "IsRHDOverride"):
          self.params.remove(k)
        self.params.put_bool("OnroadCycleRequested", True)
        return {"ok": True}
      if act == "reboot":
        # HARDWARE.reboot() is immediate (no manager deferral), so only while the car is off / the comma is offroad
        if self.started or self._onroad():
          return {"ok": False, "error": "Reboot is available while the car is off."}
        self.params.put_bool("DoUserReboot", True)
        try:
          from openpilot.system.hardware import HARDWARE
          threading.Timer(1.0, HARDWARE.reboot).start()
        except Exception:
          self.params.put_bool("DoReboot", True)
        return {"ok": True, "rebooting": True}
      if act == "power_off":
        if self.engaged:
          return {"ok": False, "error": "Disengage to Power Off"}
        if self.openpilot_down:  # the manager reads DoShutdown; with openpilot stopped nobody would
          from openpilot.system.hardware import HARDWARE
          threading.Timer(1.0, HARDWARE.shutdown).start()
        else:
          self.params.put_bool_nonblocking("DoShutdown", True)
        return {"ok": True, "shutdown": True}
      if act == "galaxy_pair":
        r = self.ops.galaxy_pair(req.get("password", ""))
        self.ops_info_dirty()
        return r
      if act == "galaxy_unpair":
        r = self.ops.galaxy_unpair()
        self.ops_info_dirty()
        return r
      if act == "galaxy_url":
        return {"ok": bool(self.ops._galaxy_url()), "url": self.ops._galaxy_url(), "error": "Galaxy is not paired yet."}
      if act == "comma_pair_url":
        return self.ops.comma_pair_url()
      # ---- Developer panel ----
      if act == "ssh_add":
        r = self.ops.ssh_add(req.get("username", ""))
        self.ops_info_dirty()
        return r
      if act == "ssh_remove":
        r = self.ops.ssh_remove()
        self.ops_info_dirty()
        return r
      # ---- Network panel ----
      if act == "wifi_status":
        return self.ops.wifi_status()
      if act == "wifi_scan":
        return self.ops.wifi_scan(bool(req.get("rescan", False)))
      if act == "wifi_connect":
        return self.ops.wifi_connect(req.get("ssid", ""), req.get("password", ""), bool(req.get("hidden", False)))
      if act == "wifi_forget":
        return self.ops.wifi_forget(req.get("ssid", ""))
      if act == "wifi_metered":
        return self.ops.wifi_metered(req.get("mode", "unknown"))
      if act == "tether":
        return self.ops.tether(bool(req.get("on", False)))
      if act == "tether_password":
        return self.ops.tether_password(req.get("password"))
      # ---- Software panel ----
      if act == "update_check":
        return self.ops.update_check()
      if act == "update_download":
        if self.started:
          return {"ok": False, "error": "Updates are only downloaded while the car is off."}
        return self.ops.update_download()
      if act == "update_install":
        self.params.put_bool("DoReboot", True)
        return {"ok": True, "rebooting": True}
      if act == "error_log":
        return self.ops.error_log()
      if act == "exp_toggle":
        # mirrors selfdrive/ui/onroad/exp_button.py
        # same guard as the mici home-screen long-press: SafeMode blocks it; no confirmation gate
        if self.params.get_bool("SafeMode"):
          return {"ok": False, "error": "experimental mode blocked by SafeMode"}
        cur = self.params.get_bool("ExperimentalMode")
        if self.params.get_bool("ConditionalExperimental"):
          from openpilot.starpilot.common.experimental_state import CEStatus, next_manual_ce_status, sync_manual_ce_state
          status = self.params_mem.get_int("CEStatus", default=CEStatus["OFF"])
          new = next_manual_ce_status(status, cur)
          self.params_mem.put_int("CEStatus", new)
          sync_manual_ce_state(self.params, new)
          return {"ok": True, "CEStatus": new}
        self.params.put_bool("ExperimentalMode", not cur)
        return {"ok": True, "ExperimentalMode": not cur}
      if act == "nav_cancel":
        for p, k in ((self.params, "NavDestination"), (self.params_mem, "NavInstructionState"), (self.params_mem, "NavInstructionCollapsed")):
          try:
            p.remove(k)
          except Exception:
            pass
        return {"ok": True}
      if act == "tablet_mode":  # {"on": bool} -- comma screen off while this tablet shows the road view
        self.tablet_mode = bool(req.get("on", True))
        self._flag_t = 0.0
        return {"ok": True, "tablet_mode": self.tablet_mode}
      if act == "drive_state":  # "default" | "onroad" | "offroad"  (same as Galaxy / settings Force drive state)
        mode = req.get("mode", "default")
        # switching onroad/offroad starts or stops openpilot: never while engaged or while the car is moving
        if self.engaged:
          return {"ok": False, "error": "Disengage to change the drive state."}
        if self._moving():
          return {"ok": False, "error": "Stop the car to change the drive state."}
        self.params.put_bool("ForceOnroad", mode == "onroad")
        self.params.put_bool("ForceOffroad", mode == "offroad")
        return {"ok": True, "mode": mode}
      return {"ok": False, "error": f"unknown action {act}"}
    except Exception as e:
      cloudlog.exception("starview: control")
      return {"ok": False, "error": str(e)}

  # ---- client registry ----
  def add_data(self, c):
    with self.lock:
      self.data_clients.add(c)
    self.recompute_wanted()

  def remove_data(self, c):
    with self.lock:
      self.data_clients.discard(c)
    self.recompute_wanted()

  def add_video(self, c):
    with self.lock:
      self.video_clients[c.cam].add(c)
    self.recompute_wanted()
    self.request_keyframe()

  def remove_video(self, c):
    with self.lock:
      self.video_clients[c.cam].discard(c)
    self.recompute_wanted()

  def status(self):
    with self.lock:
      return {
        "uptime_s": round(time.monotonic() - self.t_start, 1),
        "cpu_pct_one_core": round(self.cpu_pct, 2),
        "cpu_pct_of_device": round(self.cpu_pct / (os.cpu_count() or 1), 2),
        "live_streaming": self.live,
        "drive_hold": self.drive_hold,
        "enc_stall_s": round(self.enc_stall_s),
        "enc_ok_for_s": round(time.monotonic() - self._enc_ok_since) if self._enc_ok_since is not None else 0,
        "data_clients": [{"peer": c.peer, "sent": c.sent, "dropped": c.dropped, "queued": c.q.qsize(),
                          "rates": c.rates} for c in self.data_clients],
        "video_clients": {cam: [{"sent": c.sent, "dropped": c.dropped, "need_key": c.need_key} for c in cs]
                          for cam, cs in self.video_clients.items() if cs},
        "subscribed": sorted(self.socks),
        "rates": {k: {"hz": round(v[0], 1), "kBps": round(v[1] / 1000, 1)} for k, v in self.rates_out.items()},
        "totals": {k: dict(v) for k, v in self.stats.items()},
      }


# ------------------------------------------------------------------------------------------ http
def hello(hub: Hub):
  meta = {}
  try:
    if get_build_metadata:
      bm = get_build_metadata()
      meta = {"version": bm.openpilot.version, "branch": bm.channel, "commit": bm.openpilot.git_commit[:8]}
  except Exception:
    pass
  p = hub.params
  try:
    dongle = p.get("DongleId", encoding="utf8") or ""
  except Exception:
    dongle = ""
  return {
    "type": "hello", "proto": 2, "port": PORT, "dongleId": dongle, **meta,
    "t_ns": time.monotonic_ns(), "unix_ns": time.time_ns(),
    "services": {k: SERVICE_LIST[k].frequency for k in SERVICE_LIST},
    "defaults": DEFAULT_RATES,
    "video": {cam: f"/video/{cam}" for cam in VIDEO_CAMS},
    "video_header": "<IQQIHH frameId,timestampEof,unixTimestampNanos,flags(bit0=key),width,height",
  }


async def ws_data(request):
  require_paired(request)
  hub: Hub = request.app["hub"]
  ws = web.WebSocketResponse(max_msg_size=0, heartbeat=10, compress=False)
  await ws.prepare(request)
  c = DataClient(ws, asyncio.get_running_loop())
  c.peer = request.remote
  await ws.send_str(json.dumps(hello(hub)))
  hub.add_data(c)
  cloudlog.info(f"starview: data client {c.peer} connected")

  async def sender():
    while True:
      frame = await c.q.get()
      await ws.send_bytes(frame)

  async def params_pusher():
    last = None
    while True:
      try:
        snap = await asyncio.get_running_loop().run_in_executor(None, hub.params_snapshot)
        js = json.dumps(snap, default=str)
        if js != last or c.force_params:
          c.force_params = False
          last = js
          await ws.send_str(js)
      except Exception as e:
        cloudlog.warning(f"starview: params push: {e}")
      await asyncio.sleep(1.0)

  task = asyncio.create_task(sender())
  ptask = asyncio.create_task(params_pusher())
  try:
    async for msg in ws:
      if msg.type == web.WSMsgType.TEXT:
        try:
          req = json.loads(msg.data)
        except Exception:
          continue
        if "subscribe" in req and isinstance(req["subscribe"], dict):
          new = {}
          for k, v in req["subscribe"].items():
            if k in SERVICE_LIST and isinstance(v, (int, float)) and v >= 0:
              new[k] = float(v)
          c.rates = new
          hub.recompute_wanted()
          await ws.send_str(json.dumps({"type": "subscribed", "services": c.rates}))
        elif "ping" in req:
          await ws.send_str(json.dumps({"pong": req["ping"], "t_ns": time.monotonic_ns(), "unix_ns": time.time_ns()}))
        elif "action" in req:
          ack = await asyncio.get_running_loop().run_in_executor(None, hub.control, req)
          ack["type"] = "ack"
          ack["id"] = req.get("id")
          await ws.send_str(json.dumps(ack))
          hub.invalidate_params_snapshot()
          c.force_params = True
        elif req.get("get") == "params":
          c.force_params = True
      elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
        break
  finally:
    task.cancel()
    ptask.cancel()
    hub.remove_data(c)
    cloudlog.info(f"starview: data client {c.peer} gone (sent={c.sent} dropped={c.dropped})")
  return ws


async def ws_video(request):
  require_paired(request)
  hub: Hub = request.app["hub"]
  cam = request.match_info["cam"]
  if cam not in VIDEO_CAMS:
    raise web.HTTPNotFound(text=f"unknown camera {cam}; use one of {list(VIDEO_CAMS)}")
  ws = web.WebSocketResponse(max_msg_size=0, heartbeat=10, compress=False)
  await ws.prepare(request)
  c = VideoClient(ws, cam)
  hub.add_video(c)
  cloudlog.info(f"starview: video[{cam}] client {request.remote} connected")

  async def sender():
    while True:
      frame = await c.q.get()
      await ws.send_bytes(frame)
      c.sent += 1

  task = asyncio.create_task(sender())
  try:
    async for msg in ws:
      if msg.type == web.WSMsgType.TEXT and "keyframe" in msg.data:
        c.need_key = True
        hub.request_keyframe()
      elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
        break
  finally:
    task.cancel()
    hub.remove_video(c)
    cloudlog.info(f"starview: video[{cam}] client {request.remote} gone (sent={c.sent} dropped={c.dropped})")
  return ws


# ------------------------------------------------------------------------------------------ access
# Every endpoint except UDP discovery, /regulatory, /hello and /auth needs one of: this device itself, a tablet on the
# USB tether (until "usb needs key" is switched on in the comma's StarView settings), or a tablet paired by scanning
# the QR code there. A paired tablet never sends the key: it gets a session pass from /hello + /auth (pairing.py) and
# sends that (header X-StarView-Session, or ?s= from the terminal WebView). Only from private / link-local addresses.
# Ethernet links are not trusted like the tether. STARVIEW_TERM_ANY=1 (debug only) accepts any peer.
TERM_ANY = os.getenv("STARVIEW_TERM_ANY", "0") == "1"


def _usb_subnets():
  return pairing.usb_subnets()


def _request_session(request) -> str:
  return request.headers.get(pairing.SESSION_HEADER, "") or request.query.get(pairing.SESSION_QUERY, "")


def term_allowed(request) -> bool:
  return TERM_ANY or pairing.is_authorized(request.remote or "", _request_session(request))


def require_paired(request):
  if not term_allowed(request):
    raise web.HTTPUnauthorized(text=json.dumps({"error": "pairing required",
                                                "hint": "scan the StarView QR code in the comma's settings"}),
                               content_type="application/json")


def _require_local(request) -> str:
  remote = request.remote or ""
  if not pairing.is_local_peer(remote):
    raise web.HTTPForbidden(text="StarView pairing works on the comma's own networks only")
  return remote


async def http_hello(request):
  remote = _require_local(request)
  r = pairing.challenge(remote, request.app["hub"].ops.dongle_id(), request.query.get("n"))
  if r is None:
    raise web.HTTPBadRequest(text="n: 32-128 lowercase hex characters")
  return web.json_response(r)


async def http_auth(request):
  remote = _require_local(request)
  try:
    j = await request.json()
  except Exception:
    raise web.HTTPBadRequest(text="JSON body expected") from None
  if not isinstance(j, dict):
    raise web.HTTPBadRequest(text="JSON object expected")
  token = pairing.open_session(remote, request.app["hub"].ops.dongle_id(), j.get("sn"), j.get("cn"), j.get("proof"))
  if token is None:
    cloudlog.warning(f"starview: pairing proof refused from {remote}")
    raise web.HTTPUnauthorized(text=json.dumps({"error": "pairing required",
                                                "hint": "scan the StarView QR code in the comma's settings"}),
                               content_type="application/json")
  return web.json_response({"session": token, "expires_s": int(pairing.SESSION_TTL_S)})


# ------------------------------------------------------------------------------------------ terminal
# WS /term: a login shell in a pty (binary frames both ways; text {"resize":[cols,rows]}).  Full shell access; same
# access rule as every other endpoint (see above).


# Sessions outlive the WebSocket: leaving the tablet app (or a flaky link) only DETACHES; the shell and whatever runs in
# it keep going, and reconnecting with the same ?sid= reattaches and replays the recent screen output.  A session ends
# when its shell exits, on {"kill": true}, or after TERM_IDLE_S detached.
TERM_IDLE_S = 12 * 3600
TERM_BUF = 256 * 1024
TERM_SESSIONS: dict = {}


class TermSession:
  def __init__(self, sid: str, remote: str):
    self.sid = sid; self.buf = bytearray(); self.ws = None; self.q = None; self.pump = None
    self.detached_at = time.monotonic(); self.alive = True
    env = dict(os.environ, TERM="xterm-256color", COLORTERM="truecolor", LANG="C.UTF-8", STARVIEW="1")
    env.pop("PYTHONPATH", None)
    home = env.get("HOME") or "/home/comma"
    cwd = "/data/openpilot" if os.path.isdir("/data/openpilot") else home
    # NOTE: never pty.fork()/os.fork() here -- forking this multi-threaded daemon from Python wedged it (the parent
    # hung on at-fork hooks and the whole bridge went silent).  subprocess spawns in C, bypassing all of that; the
    # pty comes from openpty() and `setsid --ctty` makes it the shell's controlling terminal (job control, ^C).
    self.fd, slave = pty.openpty()
    try:
      fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
    except Exception:
      pass
    argv = ["setsid", "--ctty", "/bin/bash", "-l"] if shutil.which("setsid") else ["/bin/bash", "-l"]
    try:
      self.proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, cwd=cwd, env=env, close_fds=True,
                                   start_new_session=(argv[0] != "setsid"))
    except Exception:
      os.close(self.fd); os.close(slave)
      raise
    os.close(slave)
    self.pid = self.proc.pid
    fl = fcntl.fcntl(self.fd, fcntl.F_GETFL)
    fcntl.fcntl(self.fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
    self.loop = asyncio.get_running_loop()
    self.loop.add_reader(self.fd, self._readable)
    cloudlog.info(f"starview: terminal session {sid} for {remote} (pid {self.pid})")

  def _readable(self):
    try:
      data = os.read(self.fd, 65536)
    except BlockingIOError:
      return
    except OSError:
      data = b""
    if data:
      self.buf += data
      if len(self.buf) > TERM_BUF:
        del self.buf[:len(self.buf) - TERM_BUF]
    if self.q is not None:
      self.q.put_nowait(data)
    if not data:
      self.close()

  def attach(self, ws):
    """Take over the session for this socket (an older socket, if any, is dropped) and replay the recent output."""
    if self.pump is not None:
      self.pump.cancel()
    old = self.ws
    self.ws = ws; self.q = asyncio.Queue()
    snapshot = bytes(self.buf)
    if old is not None and old is not ws:
      asyncio.ensure_future(old.close(message=b"taken over"))

    async def pump():
      try:
        await ws.send_str(json.dumps({"session": self.sid, "replay": len(snapshot)}))
        if snapshot:
          await ws.send_bytes(snapshot)
        while True:
          data = await self.q.get()
          if not data:
            await ws.close(message=b"shell exited")
            return
          await ws.send_bytes(data)
      except (asyncio.CancelledError, ConnectionResetError):
        pass
      except Exception as e:
        cloudlog.warning(f"starview: terminal pump: {e}")

    self.pump = asyncio.create_task(pump())

  def detach(self, ws):
    if self.ws is ws:
      if self.pump is not None:
        self.pump.cancel()
      self.ws = None; self.q = None; self.pump = None
      self.detached_at = time.monotonic()

  def write(self, data: bytes):
    try:
      os.write(self.fd, data)
    except OSError:
      pass

  def resize(self, cols: int, rows: int):
    try:
      fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
      os.kill(self.pid, signal.SIGWINCH)
    except Exception:
      pass

  def close(self):
    if not self.alive:
      return
    self.alive = False
    TERM_SESSIONS.pop(self.sid, None)
    try:
      self.loop.remove_reader(self.fd)
    except Exception:
      pass
    if self.q is not None:
      self.q.put_nowait(b"")

    def reap():
      for sig in (signal.SIGHUP, signal.SIGKILL):
        if self.proc.poll() is not None:
          break
        try:
          os.killpg(self.pid, sig)  # bash is the session/group leader either way (setsid exec's in place)
        except Exception:
          try:
            self.proc.send_signal(sig)
          except Exception:
            pass
        time.sleep(0.3)
      try:
        self.proc.wait(timeout=1)
      except Exception:
        pass
      try:
        os.close(self.fd)
      except Exception:
        pass
    threading.Thread(target=reap, daemon=True).start()
    cloudlog.info(f"starview: terminal session {self.sid} ended")


async def term_reaper():
  while True:
    await asyncio.sleep(60)
    now = time.monotonic()
    for s in list(TERM_SESSIONS.values()):
      if s.proc.poll() is not None or (s.ws is None and now - s.detached_at > TERM_IDLE_S):
        s.close()


async def ws_term(request):
  if not term_allowed(request):
    raise web.HTTPForbidden(text="terminal: USB link, or Wi-Fi with the key from a USB pairing")
  ws = web.WebSocketResponse(max_msg_size=0, heartbeat=30, compress=False)
  await ws.prepare(request)
  if not getattr(term_reaper, "started", False):
    term_reaper.started = True
    asyncio.create_task(term_reaper())
  sid = "".join(c for c in request.query.get("sid", "") if c.isalnum())[:40] or secrets.token_hex(8)
  sess = TERM_SESSIONS.get(sid)
  if sess is None or not sess.alive or sess.proc.poll() is not None:
    try:
      sess = TermSession(sid, request.remote or "")
    except Exception as e:
      await ws.send_str(json.dumps({"error": f"cannot start shell: {e}"}))
      await ws.close()
      return ws
    TERM_SESSIONS[sid] = sess
  else:
    cloudlog.info(f"starview: terminal session {sid} reattached by {request.remote}")
  sess.attach(ws)
  try:
    async for msg in ws:
      if msg.type == web.WSMsgType.BINARY:
        sess.write(msg.data)
      elif msg.type == web.WSMsgType.TEXT:
        try:
          j = json.loads(msg.data)
          if "resize" in j:
            sess.resize(int(j["resize"][0]), int(j["resize"][1]))
          elif "input" in j:
            sess.write(str(j["input"]).encode())
          elif j.get("kill"):
            sess.close()
            break
        except Exception:
          pass
      elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
        break
  finally:
    sess.detach(ws)
  return ws


# ------------------------------------------------------------------------------------------ files
# Plain HTTP file API for the tablet's Files screen (same USB-link-only guard as the terminal):
#   GET  /fs/list?path=/data            -> {"path", "entries":[{name, dir, size, mtime}]}
#   GET  /fs/walk?path=/data/x          -> {"files":[{rel, size}], "dirs":[rel...]}  (for folder copies, file by file)
#   GET  /fs/get?path=/data/x/f.bin     -> file body (streamed)
#   PUT  /fs/put?path=/data/x/f.bin     -> body written to a temp file, then renamed into place
#   POST /fs/op  {"op":"mkdir"|"rename"|"delete", "path":..., "to":...}
def _fs_guard(request):
  if not term_allowed(request):
    raise web.HTTPForbidden(text="file access: USB link, or Wi-Fi with the key from a USB pairing")


def _fs_path(request, key="path"):
  p = request.query.get(key) or (request.get("_json") or {}).get(key) or ""
  if not p.startswith("/"):
    raise web.HTTPBadRequest(text="absolute path required")
  return os.path.normpath(p)


def _fs_list(path):
  entries = []
  with os.scandir(path) as it:
    for e in it:
      try:
        st = e.stat(follow_symlinks=True)
        entries.append({"name": e.name, "dir": e.is_dir(follow_symlinks=True), "size": st.st_size, "mtime": int(st.st_mtime)})
      except OSError:
        entries.append({"name": e.name, "dir": False, "size": 0, "mtime": 0, "broken": True})
  entries.sort(key=lambda x: (not x["dir"], x["name"].lower()))
  return entries


async def fs_list(request):
  _fs_guard(request)
  path = _fs_path(request)
  try:
    entries = await asyncio.get_running_loop().run_in_executor(None, _fs_list, path)
  except FileNotFoundError:
    raise web.HTTPNotFound(text=f"{path} not found")
  except PermissionError:
    raise web.HTTPForbidden(text=f"{path}: permission denied")
  except NotADirectoryError:
    raise web.HTTPBadRequest(text=f"{path} is not a directory")
  try:
    total, used, free = shutil.disk_usage(path)
  except Exception:
    total = used = free = 0
  return web.json_response({"path": path, "entries": entries, "free": free, "total": total})


def _fs_walk(path):
  files, dirs = [], []
  for root, ds, fs in os.walk(path):
    rel_root = os.path.relpath(root, path)
    for d in ds:
      dirs.append(os.path.normpath(os.path.join(rel_root, d)))
    for f in fs:
      full = os.path.join(root, f)
      try:
        files.append({"rel": os.path.normpath(os.path.join(rel_root, f)), "size": os.path.getsize(full)})
      except OSError:
        pass
  return {"files": files, "dirs": dirs}


async def fs_walk(request):
  _fs_guard(request)
  path = _fs_path(request)
  if not os.path.isdir(path):
    raise web.HTTPBadRequest(text=f"{path} is not a directory")
  return web.json_response(await asyncio.get_running_loop().run_in_executor(None, _fs_walk, path))


async def fs_get(request):
  _fs_guard(request)
  path = _fs_path(request)
  if not os.path.isfile(path):
    raise web.HTTPNotFound(text=f"{path} not found")
  return web.FileResponse(path, chunk_size=256 * 1024, headers={"Content-Disposition": f'attachment; filename="{os.path.basename(path)}"'})


async def fs_put(request):
  _fs_guard(request)
  path = _fs_path(request)
  d = os.path.dirname(path)
  os.makedirs(d, exist_ok=True)
  tmp = os.path.join(d, f".{os.path.basename(path)}.starview-part")
  n = 0
  try:
    with open(tmp, "wb") as f:
      async for chunk in request.content.iter_chunked(256 * 1024):
        f.write(chunk)
        n += len(chunk)
    os.replace(tmp, path)
  except Exception as e:
    try:
      os.unlink(tmp)
    except Exception:
      pass
    raise web.HTTPInternalServerError(text=f"write failed: {e}")
  mt = request.query.get("mtime")
  if mt:
    try:
      os.utime(path, (int(mt), int(mt)))
    except Exception:
      pass
  return web.json_response({"ok": True, "path": path, "size": n})


async def fs_op(request):
  _fs_guard(request)
  try:
    j = await request.json()
  except Exception:
    raise web.HTTPBadRequest(text="json body required")
  request["_json"] = j
  op = j.get("op")
  path = _fs_path(request)
  try:
    if op == "mkdir":
      os.makedirs(path, exist_ok=True)
    elif op == "rename":
      to = _fs_path(request, "to")
      os.rename(path, to)
    elif op == "delete":
      if path in ("/", "/data", "/data/openpilot", "/data/params"):
        raise web.HTTPForbidden(text=f"refusing to delete {path}")
      if os.path.isdir(path) and not os.path.islink(path):
        await asyncio.get_running_loop().run_in_executor(None, shutil.rmtree, path)
      else:
        os.unlink(path)
    else:
      raise web.HTTPBadRequest(text=f"unknown op {op}")
  except web.HTTPException:
    raise
  except Exception as e:
    return web.json_response({"ok": False, "error": str(e)}, status=400)
  return web.json_response({"ok": True})


_WAZE_FEED = None


def _waze_feed():
  global _WAZE_FEED
  if _WAZE_FEED is None:
    from openpilot.starpilot.navigation.waze_bridge import WazeFeed
    _WAZE_FEED = WazeFeed()
  return _WAZE_FEED


async def http_waze_post(request):
  require_paired(request)
  try:
    snap = await request.json()
  except Exception:
    raise web.HTTPBadRequest(text="JSON body expected") from None
  if not isinstance(snap, dict):
    raise web.HTTPBadRequest(text="JSON object expected")
  try:
    res = await asyncio.get_running_loop().run_in_executor(None, _waze_feed().ingest, snap)
  except Exception as e:
    cloudlog.warning(f"starview: waze ingest: {e}")
    return web.json_response({"ok": False, "error": str(e)[:300]}, status=500)
  return web.json_response(res)


async def http_waze_get(request):
  require_paired(request)
  return web.json_response(_waze_feed().status())


async def http_status(request):
  require_paired(request)
  return web.json_response(request.app["hub"].status())


def storage_info():
  """Internal log storage + the USB archive stick, for the tablet's home screen (cheap: no du, no stick access)."""
  out = {"now": time.time()}
  try:
    st = os.statvfs("/data")
    out["internal"] = {"total_GB": round(st.f_blocks * st.f_frsize / 1e9, 1), "free_GB": round(st.f_bavail * st.f_frsize / 1e9, 1)}
    rd = "/data/media/0/realdata"
    segs = [e for e in os.scandir(rd) if e.is_dir()] if os.path.isdir(rd) else []
    out["internal"]["segments"] = len(segs)
    if segs:
      newest = max(segs, key=lambda e: e.stat().st_mtime)
      oldest = min(segs, key=lambda e: e.stat().st_mtime)
      out["internal"]["newest_time"] = newest.stat().st_mtime
      out["internal"]["oldest_time"] = oldest.stat().st_mtime
  except Exception as e:
    out["internal_error"] = str(e)
  stick = {"present": os.path.exists("/dev/disk/by-label/EXTDATA"),
           "auto": not os.path.exists("/data/starview/no_log_archive")}
  try:
    stick.update(json.load(open("/data/starview/log_archive_status.json")))
  except Exception:
    pass
  out["stick"] = stick
  return out


async def http_storage(request):
  require_paired(request)
  return web.json_response(await asyncio.get_running_loop().run_in_executor(None, storage_info))


async def http_regulatory(request):
  try:
    from openpilot.common.basedir import BASEDIR
    return web.FileResponse(os.path.join(BASEDIR, "selfdrive/assets/offroad/fcc.html"))
  except Exception as e:
    return web.Response(text=f"regulatory page unavailable: {e}", status=404)


async def http_index(request):
  require_paired(request)
  return web.Response(text=TEST_PAGE, content_type="text/html")


TEST_PAGE = r"""<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>StarView probe</title>
<style>
body{font:15px system-ui,sans-serif;background:#0b0d12;color:#dfe3ea;margin:0;padding:12px}
h1{font-size:18px;margin:0 0 8px} .row{display:flex;gap:12px;flex-wrap:wrap}
.card{background:#151924;border-radius:10px;padding:10px 12px;min-width:260px;flex:1}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums} td,th{padding:2px 6px;text-align:right;border-bottom:1px solid #222a3a} td:first-child,th:first-child{text-align:left}
canvas{width:100%;background:#000;border-radius:8px} .k{color:#8fa3c7} .ok{color:#6fe3a1} .bad{color:#ff7b7b}
button{background:#263049;color:#fff;border:0;border-radius:6px;padding:6px 10px;margin-right:6px}
</style></head><body>
<h1>StarView probe <span id=st class=k>connecting…</span></h1>
<div class=row>
 <div class=card style="flex:2">
  <div><button onclick="startVideo('road')">road</button><button onclick="startVideo('wide')">wide</button><button onclick="startVideo('driver')">driver</button>
  <span id=vst class=k>video: idle</span></div>
  <canvas id=cv width=1280 height=720></canvas>
 </div>
 <div class=card>
  <div>RTT <b id=rtt>–</b> ms &nbsp; clock offset <b id=off>–</b> ms &nbsp; total <b id=tot>0</b> kB/s</div>
  <table><thead><tr><th>service</th><th>Hz</th><th>kB/s</th><th>msgs</th></tr></thead><tbody id=tb></tbody></table>
 </div>
</div>
<script>
const $=id=>document.getElementById(id);
const stats={}; let bytesWin=0, tWin=performance.now();
const ws=new WebSocket(`ws://${location.host}/data`); ws.binaryType='arraybuffer';
ws.onopen=()=>{$('st').textContent='data connected'; setInterval(()=>ws.send(JSON.stringify({ping:performance.now()})),1000);};
ws.onclose=()=>{$('st').textContent='data closed'; $('st').className='bad'};
ws.onmessage=e=>{
  if(typeof e.data==='string'){const m=JSON.parse(e.data);
    if(m.type==='hello'){$('st').textContent=`connected to ${m.dongleId||'?'} ${m.branch||''} ${m.commit||''}`; $('st').className='ok';}
    if(m.pong!==undefined){const rtt=performance.now()-m.pong; $('rtt').textContent=rtt.toFixed(1);
      $('off').textContent=((Date.now()*1e6-m.unix_ns)/1e6-rtt/2).toFixed(0);} return;}
  const b=new Uint8Array(e.data); const n=b[0]; const name=new TextDecoder().decode(b.subarray(1,1+n));
  const s=stats[name]||(stats[name]={msgs:0,bytes:0,wm:0,wb:0}); s.msgs++; s.bytes+=b.length; s.wm++; s.wb+=b.length; bytesWin+=b.length;
};
setInterval(()=>{const now=performance.now(), dt=(now-tWin)/1000; tWin=now;
  const rows=Object.entries(stats).sort().map(([k,s])=>{const r=`<tr><td>${k}</td><td>${(s.wm/dt).toFixed(1)}</td><td>${(s.wb/dt/1000).toFixed(1)}</td><td>${s.msgs}</td></tr>`; s.wm=0;s.wb=0; return r;});
  $('tb').innerHTML=rows.join(''); $('tot').textContent=(bytesWin/dt/1000).toFixed(0); bytesWin=0;},1000);

// ---- video via WebCodecs (Chrome/Android 94+) ----
let vws=null, dec=null, frames=0, vwin=0, vt=performance.now(), lat=0;
const cv=$('cv'), ctx=cv.getContext('2d');
function startVideo(cam){
  if(vws){vws.close(); vws=null;} if(dec){try{dec.close()}catch(e){} dec=null;}
  if(!('VideoDecoder' in window)){$('vst').className='bad';$('vst').textContent=`WebCodecs needs a "secure" origin. In Chrome open chrome://flags/#unsafely-treat-insecure-origin-as-secure, add http://${location.host} , enable, relaunch.`;return;}
  dec=new VideoDecoder({output:f=>{if(cv.width!==f.displayWidth){cv.width=f.displayWidth;cv.height=f.displayHeight;} ctx.drawImage(f,0,0); f.close(); frames++; vwin++;},
                        error:e=>{$('vst').textContent='decoder error: '+e; $('vst').className='bad';}});
  dec.configure({codec:'avc1.64001F', optimizeForLatency:true});
  vws=new WebSocket(`ws://${location.host}/video/${cam}`); vws.binaryType='arraybuffer';
  vws.onopen=()=>{$('vst').textContent=`video[${cam}] connected, waiting for keyframe…`; $('vst').className='k'};
  vws.onclose=()=>{$('vst').textContent='video closed'; $('vst').className='bad'};
  vws.onmessage=e=>{const dv=new DataView(e.data);
    const frameId=dv.getUint32(0,true), unixNs=dv.getBigUint64(12,true), flags=dv.getUint32(20,true), w=dv.getUint16(24,true), h=dv.getUint16(26,true);
    lat=Number(BigInt(Date.now())*1000000n-unixNs)/1e6;
    if(dec.state!=='configured')return;
    if(dec.decodeQueueSize>4)return; // drop if the tablet can't keep up
    dec.decode(new EncodedVideoChunk({type:(flags&1)?'key':'delta', timestamp:frameId*50000, data:new Uint8Array(e.data,28)}));
  };
  setInterval(()=>{const now=performance.now(),dt=(now-vt)/1000;vt=now; if(vws&&vws.readyState===1){$('vst').textContent=`video[${cam}] ${(vwin/dt).toFixed(1)} fps, ${cv.width}x${cv.height}, encode→tablet ≈ ${lat.toFixed(0)} ms (needs synced clocks)`; $('vst').className='ok'; vwin=0;}},1000);
}
</script></body></html>"""


DISCOVERY_PORT = int(os.getenv("STARVIEW_DISCOVERY_PORT", "8091"))
DISCOVERY_MAGIC = b"STARVIEW?"


def discovery_thread(hub: Hub):
  """Answer UDP broadcasts 'STARVIEW?' on :8091 with a JSON identity, so the tablet finds us without a fixed IP.
  Test from any box on the link:  echo -n STARVIEW? | nc -u -b -w1 255.255.255.255 8091"""
  import socket
  s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
  s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
  try:
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
  except OSError:
    pass
  s.bind(("0.0.0.0", DISCOVERY_PORT))
  while True:
    try:
      data, addr = s.recvfrom(256)
      if not data.startswith(DISCOVERY_MAGIC):
        continue
      h = hello(hub)
      reply = {"type": "starview", "port": PORT, "dongleId": h.get("dongleId", ""),
               "branch": h.get("branch", ""), "commit": h.get("commit", ""),
               "live_streaming": hub.live, "unix_ns": time.time_ns(),
               # USB tether peers are trusted until "usb needs key" is on; everyone else must scan the QR
               "pairing_required": pairing.usb_requires_key() or not pairing.is_usb_peer(addr[0])}
      # "STARVIEW? <nonce>": prove we hold the pairing key, so a paired tablet can ignore impostors
      cn = data[len(DISCOVERY_MAGIC):].strip().decode("ascii", "replace")
      if pairing.valid_nonce(cn):
        reply["proof"] = pairing.server_proof(reply["dongleId"], cn)
      reply = json.dumps(reply).encode()
      s.sendto(reply, addr)
    except Exception as e:
      cloudlog.warning(f"starview: discovery: {e}")
      time.sleep(0.5)


# ------------------------------------------------------------------------------------------ auto USB tethering
# The tablet falls back to charging-only after the cable is re-plugged (its "default USB configuration" isn't applied).
# With USB debugging on and this comma allowed once (HOME=/data/starview/adbhome adb devices -> "Allow"), switch it back
# ourselves: `svc usb setFunctions rndis` is what the tethering toggle does.  Touch /data/starview/no_auto_tether to disable.
ADB_HOME = "/data/starview/adbhome"
AUTO_TETHER_OFF = "/data/starview/no_auto_tether"


def _usb_link_up() -> bool:
  return bool(_usb_subnets())


def auto_tether_thread():
  adb = shutil.which("adb")
  if adb is None or not os.path.isdir(ADB_HOME):
    cloudlog.info("starview: auto-tether off (no adb or no paired key in /data/starview/adbhome)")
    return
  env = dict(os.environ, HOME=ADB_HOME)
  last_try = 0.0
  while True:
    time.sleep(5)
    try:
      if os.path.exists(AUTO_TETHER_OFF) or _usb_link_up() or time.monotonic() - last_try < 20:
        continue
      r = subprocess.run([adb, "devices"], capture_output=True, text=True, timeout=10, env=env)
      serials = [ln.split()[0] for ln in r.stdout.splitlines()[1:] if ln.strip().endswith("device")]
      if not serials:
        continue
      last_try = time.monotonic()
      for sn in serials:
        cur = subprocess.run([adb, "-s", sn, "shell", "svc", "usb", "getFunctions"], capture_output=True, text=True, timeout=10, env=env)
        if "rndis" in cur.stdout:
          continue
        subprocess.run([adb, "-s", sn, "shell", "svc", "usb", "setFunctions", "rndis"], capture_output=True, text=True, timeout=10, env=env)
        cloudlog.warning(f"starview: tablet {sn} was not tethering ({cur.stdout.strip() or 'none'}); switched USB tethering on")
    except Exception as e:
      cloudlog.warning(f"starview: auto-tether: {e}")


# ------------------------------------------------------------------------------------------ tablet screen follows the car
# Car off (ignition off, from the panda - NOT "force offroad") for TABLET_SLEEP_S -> tablet screen off; car on -> screen
# on and StarView in front. Uses the same adb pairing as auto-tethering. Acts on ignition changes only, so it never
# fights you if you wake the tablet by hand while parked.  Off: touch /data/starview/no_tablet_power.
# Delay: echo 60 > /data/starview/tablet_sleep_delay  (seconds, default 30).
TABLET_POWER_OFF = "/data/starview/no_tablet_power"
TABLET_SLEEP_DELAY = "/data/starview/tablet_sleep_delay"


def _adb(*args, timeout=10):
  adb = shutil.which("adb")
  if adb is None:
    return None
  env = dict(os.environ, HOME=ADB_HOME)
  r = subprocess.run([adb, "devices"], capture_output=True, text=True, timeout=timeout, env=env)
  serials = [ln.split()[0] for ln in r.stdout.splitlines()[1:] if ln.strip().endswith("device")]
  if not serials:
    return None
  return subprocess.run([adb, "-s", serials[0], *args], capture_output=True, text=True, timeout=timeout, env=env)


def _tablet_awake():
  r = _adb("shell", "dumpsys", "power")
  if r is None:
    return None
  for ln in r.stdout.splitlines():
    if "mWakefulness=" in ln:
      return "Awake" in ln
  return None


def tablet_power_thread():
  if shutil.which("adb") is None or not os.path.isdir(ADB_HOME):
    cloudlog.info("starview: tablet power follow off (no adb or no paired key)")
    return
  sock = messaging.sub_sock("pandaStates", conflate=True)
  ignition = None; last_rx = 0.0; off_since = None
  want = None          # "wake" / "sleep": a pending action, retried until the tablet confirms it
  while True:
    time.sleep(1)
    try:
      b = messaging.recv_one_or_none(sock)
      now = time.monotonic()
      if b is not None:
        last_rx = now
        ps = b.pandaStates
        ign = any(p.ignitionLine or p.ignitionCan for p in ps) if len(ps) else False
        if ign != ignition:
          if ignition is not None or ign:   # first reading: only act if the car is on (the comma just booted with it)
            cloudlog.info(f"starview: ignition {'ON' if ign else 'OFF'}")
            want = "wake" if ign else None
          off_since = None if (ign or ignition is None) else now   # a real ON->OFF change, not the first reading
          ignition = ign
      if now - last_rx > 10 or os.path.exists(TABLET_POWER_OFF):
        continue
      if ignition is False and off_since is not None:
        try:
          delay = float(open(TABLET_SLEEP_DELAY).read().strip())
        except Exception:
          delay = 30.0
        if now - off_since >= delay:
          want = "sleep"; off_since = None
      if want is None:
        continue
      awake = _tablet_awake()
      if awake is None:
        continue                          # tablet not reachable yet (booting / tethering coming up): try again
      if want == "wake":
        if not awake:
          _adb("shell", "input", "keyevent", "KEYCODE_WAKEUP")
        _adb("shell", "am", "start", "-n", "com.starpilot.starview/.MainActivity")
        cloudlog.warning("starview: car on -> tablet screen on")
      elif want == "sleep" and awake:
        _adb("shell", "input", "keyevent", "KEYCODE_SLEEP")
        cloudlog.warning("starview: car off -> tablet screen off")
      want = None
    except Exception as e:
      cloudlog.warning(f"starview: tablet power: {e}")


def tablet_flag_thread(hub):
  """Own 1 s heartbeat for the tablet-mode flag, so a busy reader thread (video fan-out on a loaded comma) can't let
  the flag go stale and wake the comma screen (the UI treats a flag older than 5 s as 'tablet gone')."""
  while True:
    try:
      hub._flag_t = 0.0
      hub.tablet_flag_tick(time.monotonic())
    except Exception as e:
      cloudlog.warning(f"starview: tablet flag: {e}")
    try:
      hub.openpilot_tick()
    except Exception as e:
      cloudlog.warning(f"starview: openpilot check: {e}")
    try:
      hub.video_health_tick()
    except Exception as e:
      cloudlog.warning(f"starview: video health: {e}")
    try:
      hub.drive_hold_tick()
    except Exception as e:
      cloudlog.warning(f"starview: drive hold: {e}")
    time.sleep(1.0)


def log_archive_thread():
  """Parked only: copy finished drives from internal storage to the USB stick (logarchive.py mounts, copies, unmounts).
  loggerd itself always records to internal storage - a stalling stick can never reach the driving processes."""
  # bundled with this file; the old installer copied it to /data/starview instead
  bundled = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logarchive.py")
  script = bundled if os.path.exists(bundled) else "/data/starview/logarchive.py"
  time.sleep(90)
  while True:
    try:
      if os.path.exists(script) and not os.path.exists("/data/starview/no_log_archive") and \
         Params().get("IsOnroad") not in (b"1", "1", True, 1):
        subprocess.run(["nice", "-n", "19", "ionice", "-c3", "python3", script, "--once"],
                       capture_output=True, timeout=4 * 3600)
    except Exception as e:
      cloudlog.warning(f"starview: log archive: {e}")
    time.sleep(300)


def main():
  import atexit
  atexit.register(lambda: os.path.exists(Hub.TABLET_FLAG) and os.unlink(Hub.TABLET_FLAG))
  loop = asyncio.new_event_loop()
  asyncio.set_event_loop(loop)
  app = web.Application(client_max_size=1024 * 1024)
  app["hub"] = Hub(loop)
  threading.Thread(target=discovery_thread, args=(app["hub"],), name="starview-discovery", daemon=True).start()
  threading.Thread(target=auto_tether_thread, name="starview-tether", daemon=True).start()
  threading.Thread(target=tablet_power_thread, name="starview-tabletpower", daemon=True).start()
  threading.Thread(target=log_archive_thread, name="starview-logarchive", daemon=True).start()
  threading.Thread(target=tablet_flag_thread, args=(app["hub"],), name="starview-tabletflag", daemon=True).start()
  app.add_routes([
    web.get("/", http_index),
    web.get("/status", http_status),
    web.get("/storage", http_storage),
    web.get("/regulatory", http_regulatory),
    web.get("/term", ws_term),
    web.get("/hello", http_hello),
    web.post("/auth", http_auth),
    web.post("/waze", http_waze_post),
    web.get("/waze", http_waze_get),
    web.get("/fs/list", fs_list),
    web.get("/fs/walk", fs_walk),
    web.get("/fs/get", fs_get),
    web.put("/fs/put", fs_put),
    web.post("/fs/op", fs_op),
    web.get("/data", ws_data),
    web.get("/video/{cam}", ws_video),
  ])
  cloudlog.info(f"starview: listening on {HOST}:{PORT}")
  print(f"starviewd listening on http://{HOST}:{PORT}/", flush=True)
  web.run_app(app, host=HOST, port=PORT, loop=loop, print=None, access_log=None)


if __name__ == "__main__":
  main()
