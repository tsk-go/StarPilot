import asyncio
import hashlib
import hmac
import json
import secrets

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from openpilot.starpilot.system.starview import pairing, starviewd

DONGLE = "0123456789abcdef"


class FakeOps:
  def dongle_id(self):
    return DONGLE


class FakeHub:
  ops = FakeOps()


def sign(key, *parts):
  return hmac.new(key.encode(), "|".join(parts).encode(), hashlib.sha256).hexdigest()


@pytest.fixture(autouse=True)
def starview_dir(tmp_path, monkeypatch):
  monkeypatch.setattr(pairing, "STARVIEW_DIR", tmp_path)
  monkeypatch.setattr(pairing, "KEY_FILE", tmp_path / "termkey")
  monkeypatch.setattr(pairing, "REQUIRE_KEY_ON_USB_FLAG", tmp_path / "require_pairing")
  monkeypatch.setattr(pairing, "_usb_subnets_cache", (float("inf"), []))  # no USB tether
  pairing.end_sessions()
  yield
  pairing.end_sessions()


async def guarded(request):
  starviewd.require_paired(request)
  return web.json_response({"ok": True})


def run(remote, steps):
  """Serve the real /hello and /auth (plus a guarded endpoint) as if every request came from `remote`."""
  @web.middleware
  async def as_remote(request, handler):
    return await handler(request.clone(remote=remote))

  async def main():
    app = web.Application(middlewares=[as_remote])
    app["hub"] = FakeHub()
    app.add_routes([web.get("/hello", starviewd.http_hello), web.post("/auth", starviewd.http_auth), web.get("/data", guarded)])
    async with TestClient(TestServer(app)) as client:
      return await steps(client)
  return asyncio.run(main())


async def handshake(client, key):
  cn = secrets.token_hex(16)
  r = await client.get("/hello", params={"n": cn})
  assert r.status == 200
  h = await r.json()
  assert h["dongleId"] == DONGLE
  proof_ok = hmac.compare_digest(h["proof"], sign(key, "srv", DONGLE, cn, h["sn"]))
  r = await client.post("/auth", json={"sn": h["sn"], "cn": cn, "proof": sign(key, "cli", DONGLE, h["sn"], cn)})
  return proof_ok, r.status, (await r.json()) if r.status == 200 else None


def test_paired_tablet_gets_a_pass_and_uses_it():
  async def steps(client):
    assert (await client.get("/data")).status == 401
    proof_ok, status, body = await handshake(client, pairing.get_key())
    assert proof_ok and status == 200 and body["expires_s"] > 0
    assert (await client.get("/data", headers={pairing.SESSION_HEADER: body["session"]})).status == 200
    assert (await client.get("/data", params={"s": body["session"]})).status == 200  # terminal WebView
    assert (await client.get("/data", headers={pairing.SESSION_HEADER: "nope"})).status == 401
  run("192.168.1.20", steps)


def test_raw_key_is_no_longer_accepted():
  async def steps(client):
    key = pairing.get_key()
    assert (await client.get("/data", headers={"X-StarView-Key": key})).status == 401
    assert (await client.get("/data", params={"key": key})).status == 401
  run("192.168.1.20", steps)


def test_tablet_with_an_old_key_is_refused():
  async def steps(client):
    proof_ok, status, _ = await handshake(client, "f" * 48)
    assert not proof_ok  # the tablet sees the comma doesn't have its key and would stop here
    assert status == 401
  run("192.168.1.20", steps)


def test_public_address_cannot_pair():
  async def steps(client):
    assert (await client.get("/hello", params={"n": secrets.token_hex(16)})).status == 403
    assert (await client.post("/auth", json={})).status == 403
  run("8.8.8.8", steps)


@pytest.mark.parametrize("body", ["not json", "[]", json.dumps({"sn": 1, "cn": [], "proof": None})])
def test_auth_rejects_junk(body):
  async def steps(client):
    r = await client.post("/auth", data=body, headers={"Content-Type": "application/json"})
    assert r.status in (400, 401)
  run("192.168.1.20", steps)


def test_hello_rejects_bad_nonce():
  async def steps(client):
    assert (await client.get("/hello", params={"n": "short"})).status == 400
    assert (await client.get("/hello")).status == 400
  run("192.168.1.20", steps)
