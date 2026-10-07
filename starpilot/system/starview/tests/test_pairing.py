import hashlib
import hmac
import ipaddress
import secrets
from urllib.parse import parse_qs, urlparse

import pytest

from openpilot.starpilot.system.starview import pairing

USB_NET = [ipaddress.ip_network("192.168.42.0/24")]


@pytest.fixture(autouse=True)
def starview_dir(tmp_path, monkeypatch):
  monkeypatch.setattr(pairing, "STARVIEW_DIR", tmp_path)
  monkeypatch.setattr(pairing, "KEY_FILE", tmp_path / "termkey")
  monkeypatch.setattr(pairing, "REQUIRE_KEY_ON_USB_FLAG", tmp_path / "require_pairing")
  monkeypatch.setattr(pairing, "_usb_subnets_cache", (float("-inf"), []))
  pairing.end_sessions()
  yield tmp_path
  pairing.end_sessions()


WIFI = "192.168.1.20"
DONGLE = "0123456789abcdef"


def tablet_proof(key: str, label: str, *parts: str) -> str:
  """What the app computes (Link.kt): HMAC-SHA256 over the key's text, parts joined with |."""
  return hmac.new(key.encode(), "|".join((label, *parts)).encode(), hashlib.sha256).hexdigest()


def pair(remote=WIFI, key=None):
  """Run the tablet's side of /hello + /auth; returns the session pass (or None)."""
  key = key or pairing.get_key()
  cn = secrets.token_hex(16)
  r = pairing.challenge(remote, DONGLE, cn)
  assert r["proof"] == pairing.server_proof(DONGLE, cn, r["sn"])
  return pairing.open_session(remote, DONGLE, r["sn"], cn, tablet_proof(key, "cli", DONGLE, r["sn"], cn))


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
  session = pair()
  assert pairing.session_valid(WIFI, session)
  assert pairing.rotate_key() != old
  assert not pairing.session_valid(WIFI, session)  # old passes die with the old key
  assert pair(key=old) is None                      # and the old key can't open new ones
  assert pairing.session_valid(WIFI, pair())


@pytest.mark.parametrize("remote,paired,expected", [
  ("127.0.0.1", False, True),         # the comma itself
  ("192.168.42.10", False, True),     # tablet on the USB tether (trusted while "usb needs key" is off)
  ("192.168.1.20", False, False),     # Wi-Fi without a session pass
  ("192.168.1.20", True, True),       # paired tablet on Wi-Fi
  ("192.168.43.5", True, True),       # paired tablet on the comma hotspot
  ("8.8.8.8", True, False),           # public address: never, even with a pass
  ("not-an-ip", True, False),
])
def test_access_rules(remote, paired, expected):
  session = pair(remote) if paired else "wrong"
  assert pairing.is_authorized(remote, session, subnets=USB_NET) is expected


def test_the_raw_key_is_not_a_pass():
  assert not pairing.is_authorized(WIFI, pairing.get_key(), subnets=USB_NET)


def test_usb_needs_key_switch():
  pairing.set_usb_requires_key(True)
  assert pairing.usb_requires_key()
  assert not pairing.is_authorized("192.168.42.10", "", subnets=USB_NET)
  assert pairing.is_authorized("192.168.42.10", pair("192.168.42.10"), subnets=USB_NET)
  assert pairing.is_authorized("127.0.0.1", "", subnets=USB_NET)
  pairing.set_usb_requires_key(False)
  assert pairing.is_authorized("192.168.42.10", "", subnets=USB_NET)


def test_ethernet_is_not_trusted_like_the_tether(monkeypatch):
  monkeypatch.setattr(pairing, "_ipv4_addrs", lambda: [("eth0", "10.0.0.2", "10.0.0.2/24"), ("usb0", "192.168.42.129", "192.168.42.129/24")])
  assert not pairing.is_usb_peer("10.0.0.7")
  assert pairing.is_usb_peer("192.168.42.10")


def test_usb_subnets_are_cached(monkeypatch):
  calls = []
  monkeypatch.setattr(pairing, "_ipv4_addrs", lambda: calls.append(1) or [("usb0", "192.168.42.129", "192.168.42.129/24")])
  for _ in range(10):
    assert pairing.is_usb_peer("192.168.42.10")
  assert len(calls) == 1


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


def test_impostor_proof_does_not_match():
  """A device without the key can't produce the comma's proof, so the tablet never talks to it."""
  cn = secrets.token_hex(16)
  r = pairing.challenge(WIFI, DONGLE, cn)
  assert r["proof"] == tablet_proof(pairing.get_key(), "srv", DONGLE, cn, r["sn"])
  assert r["proof"] != tablet_proof("f" * 48, "srv", DONGLE, cn, r["sn"])


def test_wrong_proof_gets_no_pass_and_burns_the_challenge():
  cn = secrets.token_hex(16)
  r = pairing.challenge(WIFI, DONGLE, cn)
  assert pairing.open_session(WIFI, DONGLE, r["sn"], cn, "0" * 64) is None
  good = tablet_proof(pairing.get_key(), "cli", DONGLE, r["sn"], cn)
  assert pairing.open_session(WIFI, DONGLE, r["sn"], cn, good) is None  # single use


def test_server_proof_cannot_be_replayed_as_client_proof():
  """Discovery and /hello sign whatever nonce anyone sends; those answers must not open a session."""
  cn = secrets.token_hex(16)
  r = pairing.challenge(WIFI, DONGLE, cn)
  # an attacker asks the comma to sign the challenge's own values with the server label
  forged = pairing.server_proof(DONGLE, r["sn"], cn)
  assert pairing.open_session(WIFI, DONGLE, r["sn"], cn, forged) is None


def test_challenge_is_bound_to_the_address():
  cn = secrets.token_hex(16)
  r = pairing.challenge(WIFI, DONGLE, cn)
  proof = tablet_proof(pairing.get_key(), "cli", DONGLE, r["sn"], cn)
  assert pairing.open_session("192.168.1.66", DONGLE, r["sn"], cn, proof) is None


def test_session_is_bound_to_the_address():
  session = pair()
  assert pairing.session_valid(WIFI, session)
  assert not pairing.session_valid("192.168.1.66", session)


def test_challenge_and_session_expire(monkeypatch):
  now = [1000.0]
  monkeypatch.setattr(pairing.time, "monotonic", lambda: now[0])
  cn = secrets.token_hex(16)
  r = pairing.challenge(WIFI, DONGLE, cn)
  now[0] += pairing.CHALLENGE_TTL_S + 1
  assert pairing.open_session(WIFI, DONGLE, r["sn"], cn, tablet_proof(pairing.get_key(), "cli", DONGLE, r["sn"], cn)) is None

  session = pair()
  now[0] += pairing.SESSION_TTL_S + 1
  assert not pairing.session_valid(WIFI, session)


@pytest.mark.parametrize("cn", ["", "abc", "Z" * 32, "a" * 31, "a" * 129, "ab|cd" * 8, None, 123])
def test_bad_nonces_are_refused(cn):
  assert pairing.challenge(WIFI, DONGLE, cn) is None


def test_challenge_and_session_tables_are_bounded():
  for _ in range(pairing.MAX_CHALLENGES * 3):
    pairing.challenge(WIFI, DONGLE, secrets.token_hex(16))
  assert len(pairing._challenges) <= pairing.MAX_CHALLENGES
  for _ in range(pairing.MAX_SESSIONS + 5):
    pair()
  assert len(pairing._sessions) <= pairing.MAX_SESSIONS
