"""StarView tablet pairing: one random key shared by the bridge (starviewd) and the comma's settings screen.

The comma shows the key as a QR code; the tablet scans it once and sends it with every connection
(header X-StarView-Key, or ?key= for WebSockets opened from a WebView). Unpairing makes a new key, which locks
out every tablet that has the old one.

While USB_TRUST_FLAG is absent (the default), a tablet on the USB tether cable is trusted without the key, so the
current app keeps working before it learns to scan the QR. Turning "usb needs key" on (creating the flag file)
requires the key everywhere.
"""
import ipaddress
import os
import secrets
import subprocess
import time
from pathlib import Path
from urllib.parse import urlencode

STARVIEW_DIR = Path(os.getenv("STARVIEW_DATA_DIR", "/data/starview"))
KEY_FILE = STARVIEW_DIR / "termkey"  # same file the older bridge used for the terminal key
REQUIRE_KEY_ON_USB_FLAG = STARVIEW_DIR / "require_pairing"
PORT = int(os.getenv("STARVIEW_PORT", "8090"))
KEY_HEADER = "X-StarView-Key"
MIN_KEY_LEN = 32

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
  """Unpair every tablet: make a new key. Tablets have to scan the new QR code."""
  key = secrets.token_hex(24)
  _write_key(key)
  return key


def key_matches(given: str | None) -> bool:
  return bool(given) and secrets.compare_digest(str(given), get_key())


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


def is_authorized(remote: str, given_key: str | None, subnets=None) -> bool:
  """Who may use the bridge: this device itself, a tablet on the USB tether (until "usb needs key" is on),
  or a paired tablet on a private network."""
  try:
    if ipaddress.ip_address(remote).is_loopback:
      return True
  except ValueError:
    return False
  if not usb_requires_key() and is_usb_peer(remote, subnets):
    return True
  return is_local_peer(remote) and key_matches(given_key)


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
