import ipaddress
from urllib.parse import parse_qs, urlparse

import pytest

from openpilot.starpilot.system.starview import pairing

USB_NET = [ipaddress.ip_network("192.168.42.0/24")]


@pytest.fixture(autouse=True)
def starview_dir(tmp_path, monkeypatch):
  monkeypatch.setattr(pairing, "STARVIEW_DIR", tmp_path)
  monkeypatch.setattr(pairing, "KEY_FILE", tmp_path / "termkey")
  monkeypatch.setattr(pairing, "REQUIRE_KEY_ON_USB_FLAG", tmp_path / "require_pairing")
  return tmp_path


def test_key_is_created_once_and_kept_private(starview_dir):
  key = pairing.get_key()
  assert len(key) >= pairing.MIN_KEY_LEN
  assert pairing.get_key() == key
  assert (starview_dir / "termkey").stat().st_mode & 0o777 == 0o600


def test_existing_terminal_key_is_reused(starview_dir):
  old = "a" * 48
  (starview_dir / "termkey").write_text(old)
  assert pairing.get_key() == old


def test_rotate_key_unpairs_old_tablets():
  old = pairing.get_key()
  new = pairing.rotate_key()
  assert new != old
  assert not pairing.key_matches(old)
  assert pairing.key_matches(new)


@pytest.mark.parametrize("remote,key_ok,expected", [
  ("127.0.0.1", False, True),         # the comma itself
  ("192.168.42.10", False, True),     # tablet on the USB tether (trusted while "usb needs key" is off)
  ("192.168.1.20", False, False),     # Wi-Fi without the key
  ("192.168.1.20", True, True),       # paired tablet on Wi-Fi
  ("192.168.43.5", True, True),       # paired tablet on the comma hotspot
  ("8.8.8.8", True, False),           # public address: never, even with the key
  ("not-an-ip", True, False),
])
def test_access_rules(remote, key_ok, expected):
  key = pairing.get_key() if key_ok else "wrong"
  assert pairing.is_authorized(remote, key, subnets=USB_NET) is expected


def test_usb_needs_key_switch():
  pairing.set_usb_requires_key(True)
  assert pairing.usb_requires_key()
  assert not pairing.is_authorized("192.168.42.10", "", subnets=USB_NET)
  assert pairing.is_authorized("192.168.42.10", pairing.get_key(), subnets=USB_NET)
  assert pairing.is_authorized("127.0.0.1", "", subnets=USB_NET)
  pairing.set_usb_requires_key(False)
  assert pairing.is_authorized("192.168.42.10", "", subnets=USB_NET)


def test_ethernet_is_not_trusted_like_the_tether(monkeypatch):
  monkeypatch.setattr(pairing, "_ipv4_addrs", lambda: [("eth0", "10.0.0.2", "10.0.0.2/24"), ("usb0", "192.168.42.129", "192.168.42.129/24")])
  assert not pairing.is_usb_peer("10.0.0.7")
  assert pairing.is_usb_peer("192.168.42.10")


def test_pairing_uri_lists_usb_first(monkeypatch):
  monkeypatch.setattr(pairing, "_ipv4_addrs", lambda: [("lo", "127.0.0.1", "127.0.0.1/8"), ("wlan0", "192.168.1.20", "192.168.1.20/24"),
                                                       ("usb0", "192.168.42.129", "192.168.42.129/24"),
                                                       ("rmnet_data0", "100.120.3.4", "100.120.3.4/30")])
  uri = urlparse(pairing.pairing_uri("abc123"))
  q = parse_qs(uri.query)
  assert (uri.scheme, uri.netloc) == ("starview", "pair")
  assert q["k"] == [pairing.get_key()]
  assert q["p"] == [str(pairing.PORT)]
  assert q["d"] == ["abc123"]
  assert q["h"] == ["192.168.42.129,192.168.1.20"]  # cellular / loopback left out
