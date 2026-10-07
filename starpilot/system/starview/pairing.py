"""StarView tablet pairing: one random key shared by the bridge (starviewd) and the comma's settings screen.

The comma shows the key as a QR code; the tablet scans it once. The key itself never goes over the network after
that. Instead, on every connect:

  1. tablet  GET  /hello?n=<cn>            comma -> {"dongleId", "sn", "proof": HMAC(key, "srv|<dongle>|<cn>|<sn>")}
     The tablet checks the proof (and the dongle id from the QR) and walks away if it's wrong, so a device that only
     pretends to be the comma learns nothing.
  2. tablet  POST /auth {sn, cn, proof: HMAC(key, "cli|<dongle>|<sn>|<cn>")}   comma -> {"session", "expires_s"}
     sn is single-use and expires after CHALLENGE_TTL_S.
  3. The session pass goes with every connection (header X-StarView-Session, or ?s= for WebSockets opened from a
     WebView). It only works from the address that opened it, expires after SESSION_TTL_S, ends when starviewd
     restarts, and stops working when the key changes ("unpair tablets").

UDP discovery answers "STARVIEW? <cn>" with the same kind of proof (empty sn), so the tablet can skip impostors
before connecting at all.

While REQUIRE_KEY_ON_USB_FLAG is absent (the default), a tablet on the USB tether cable is trusted without the key, so the
current app keeps working before it learns to scan the QR. Turning "usb needs key" on (creating the flag file)
requires the key everywhere.
"""
import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

STARVIEW_DIR = Path(os.getenv("STARVIEW_DATA_DIR", "/data/starview"))
KEY_FILE = STARVIEW_DIR / "termkey"  # same file the older bridge used for the terminal key
REQUIRE_KEY_ON_USB_FLAG = STARVIEW_DIR / "require_pairing"
PORT = int(os.getenv("STARVIEW_PORT", "8090"))
SESSION_HEADER = "X-StarView-Session"
SESSION_QUERY = "s"
MIN_KEY_LEN = 32
CHALLENGE_TTL_S = 30.0
SESSION_TTL_S = 24 * 3600.0
MAX_CHALLENGES = 64
MAX_SESSIONS = 32
_NONCE_RE = re.compile(r"[0-9a-f]{32,128}")

# Tether links only. Ethernet is deliberately not included: a USB-Ethernet adapter would make a whole LAN "trusted".
USB_IFACE_PREFIXES = ("usb", "rndis", "ncm")


def _write_key(key: str) -> None:
  STARVIEW_DIR.mkdir(parents=True, exist_ok=True)
  tmp = KEY_FILE.with_suffix(".tmp")
  tmp.write_text(key)
  os.chmod(tmp, 0o600)
  os.replace(tmp, KEY_FILE)


def get_key() -> str:
  """The current pairing key, created on first use."""
  try:
    key = KEY_FILE.read_text().strip()
    if len(key) >= MIN_KEY_LEN:
      return key
  except OSError:
    pass
  key = secrets.token_hex(24)
  try:
    _write_key(key)
  except OSError:
    pass
  return key


def rotate_key() -> str:
  """Unpair every tablet: make a new key. Tablets have to scan the new QR code; their session passes stop working
  (starviewd checks the key behind each pass on every use, since this runs in the UI process)."""
  key = secrets.token_hex(24)
  _write_key(key)
  return key


def _mac(*parts: str) -> str:
  return hmac.new(get_key().encode(), "|".join(parts).encode(), hashlib.sha256).hexdigest()


def server_proof(dongle_id: str, cn: str, sn: str = "") -> str:
  """What the comma answers to prove it holds the key (to the tablet's nonce cn)."""
  return _mac("srv", dongle_id, cn, sn)


def client_proof(dongle_id: str, sn: str, cn: str) -> str:
  """What the tablet sends to prove it holds the key. Different label, so a server proof can never be replayed as one."""
  return _mac("cli", dongle_id, sn, cn)


def valid_nonce(n) -> bool:
  return isinstance(n, str) and _NONCE_RE.fullmatch(n) is not None


def _key_id() -> str:
  return hashlib.sha256(get_key().encode()).hexdigest()


_lock = threading.Lock()
_challenges: dict[str, tuple[str, float]] = {}       # sn -> (remote, expires)
_sessions: dict[str, tuple[str, float, str]] = {}   # session -> (remote, expires, key id)


def _prune(now: float) -> None:
  for sn in [sn for sn, (_, exp) in _challenges.items() if exp < now]:
    del _challenges[sn]
  for tok in [tok for tok, (_, exp, _) in _sessions.items() if exp < now]:
    del _sessions[tok]


def challenge(remote: str, dongle_id: str, cn) -> dict | None:
  """Step 1 (GET /hello): prove we hold the key and hand out a single-use challenge for step 2."""
  if not valid_nonce(cn):
    return None
  sn = secrets.token_hex(16)
  now = time.monotonic()
  with _lock:
    _prune(now)
    while len(_challenges) >= MAX_CHALLENGES:
      del _challenges[next(iter(_challenges))]
    _challenges[sn] = (remote, now + CHALLENGE_TTL_S)
  return {"dongleId": dongle_id, "sn": sn, "proof": server_proof(dongle_id, cn, sn)}


def open_session(remote: str, dongle_id: str, sn, cn, proof) -> str | None:
  """Step 2 (POST /auth): the tablet's proof for a challenge we gave it -> a session pass, or None."""
  now = time.monotonic()
  with _lock:
    c = _challenges.pop(sn, None) if isinstance(sn, str) else None  # single use, even when the proof is wrong
  if c is None or c[0] != remote or c[1] < now or not valid_nonce(cn) or not isinstance(proof, str):
    return None
  if not hmac.compare_digest(proof, client_proof(dongle_id, sn, cn)):
    return None
  token = secrets.token_hex(24)
  with _lock:
    _prune(now)
    while len(_sessions) >= MAX_SESSIONS:
      del _sessions[next(iter(_sessions))]
    _sessions[token] = (remote, now + SESSION_TTL_S, _key_id())
  return token


def session_valid(remote: str, token: str | None) -> bool:
  if not token:
    return False
  with _lock:
    s = _sessions.get(token)
  return s is not None and s[0] == remote and s[1] >= time.monotonic() and hmac.compare_digest(s[2], _key_id())


def end_sessions() -> None:
  with _lock:
    _challenges.clear()
    _sessions.clear()


def usb_requires_key() -> bool:
  return REQUIRE_KEY_ON_USB_FLAG.exists()


def set_usb_requires_key(on: bool) -> None:
  STARVIEW_DIR.mkdir(parents=True, exist_ok=True)
  if on:
    REQUIRE_KEY_ON_USB_FLAG.touch()
  else:
    REQUIRE_KEY_ON_USB_FLAG.unlink(missing_ok=True)


def _ipv4_addrs() -> list[tuple[str, str, str]]:
  """(interface, address, cidr) for every IPv4 address."""
  out = []
  try:
    r = subprocess.run(["ip", "-4", "-o", "addr"], capture_output=True, text=True, timeout=5)
    for line in r.stdout.splitlines():
      parts = line.split()
      if len(parts) >= 4 and parts[2] == "inet":
        out.append((parts[1], parts[3].split("/")[0], parts[3]))
  except Exception:
    pass
  return out


_usb_subnets_cache: tuple[float, list] = (float("-inf"), [])
USB_SUBNETS_TTL_S = 5.0


def usb_subnets() -> list:
  """Subnets of the USB tether links. Checked on every request and discovery reply, so `ip addr` runs at most every 5 s."""
  global _usb_subnets_cache
  now = time.monotonic()
  checked_at, nets = _usb_subnets_cache
  if now - checked_at >= USB_SUBNETS_TTL_S:
    nets = [ipaddress.ip_network(cidr, strict=False) for iface, _, cidr in _ipv4_addrs() if iface.startswith(USB_IFACE_PREFIXES)]
    _usb_subnets_cache = (now, nets)
  return nets


def is_usb_peer(remote: str, subnets=None) -> bool:
  try:
    ip = ipaddress.ip_address(remote)
  except ValueError:
    return False
  if ip.is_loopback:
    return True
  return any(ip in n for n in (usb_subnets() if subnets is None else subnets))


def is_local_peer(remote: str) -> bool:
  """Private, link-local or loopback address: the only places a paired tablet may connect from."""
  try:
    ip = ipaddress.ip_address(remote)
  except ValueError:
    return False
  return ip.is_private or ip.is_link_local or ip.is_loopback


def is_authorized(remote: str, session: str | None, subnets=None) -> bool:
  """Who may use the bridge: this device itself, a tablet on the USB tether (until "usb needs key" is on),
  or a paired tablet with a session pass, on a private network."""
  try:
    if ipaddress.ip_address(remote).is_loopback:
      return True
  except ValueError:
    return False
  if not usb_requires_key() and is_usb_peer(remote, subnets):
    return True
  return is_local_peer(remote) and session_valid(remote, session)


def pairing_hosts() -> list[str]:
  """Addresses the tablet can reach the comma on: USB tether first, then Wi-Fi / hotspot."""
  addrs = [(iface, ip) for iface, ip, _ in _ipv4_addrs() if not iface.startswith("lo")]
  addrs.sort(key=lambda a: (not a[0].startswith(USB_IFACE_PREFIXES), a[0]))
  return [ip for _, ip in addrs if is_local_peer(ip)]


def pairing_uri(dongle_id: str = "") -> str:
  """What the QR code holds, e.g. starview://pair?k=<key>&p=8090&d=<dongle>&h=192.168.42.129,192.168.1.20"""
  q = {"k": get_key(), "p": PORT}
  if dongle_id:
    q["d"] = dongle_id
  hosts = pairing_hosts()
  if hosts:
    q["h"] = ",".join(hosts)
  return "starview://pair?" + urlencode(q)
