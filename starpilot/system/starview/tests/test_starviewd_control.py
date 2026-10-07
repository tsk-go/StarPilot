import threading

import pytest

from openpilot.starpilot.system.starview import starviewd


class FakeParams:
  def __init__(self, values=None):
    self.values = dict(values or {})
    self.reads = 0

  def get(self, key, encoding=None):
    self.reads += 1
    return self.values.get(key)

  def get_bool(self, key):
    return bool(self.values.get(key, False))

  def put_bool(self, key, value):
    self.values[key] = bool(value)

  def put_bool_nonblocking(self, key, value):
    self.put_bool(key, value)


class FakeOps:
  def info(self):
    return {}

  def _locked(self, key):
    return False


def make_hub(onroad=False, started=False, engaged=False):
  hub = object.__new__(starviewd.Hub)
  hub.params = FakeParams({"IsOnroad": b"1" if onroad else b"0"})
  hub.params_mem = FakeParams()
  hub.started = started
  hub.engaged = engaged
  hub.ops = FakeOps()
  hub._car_state_sock = None
  hub._snap_lock = threading.Lock()
  hub._snap = None
  hub._snap_t = 0.0
  hub._info = {}
  hub._info_t = 0.0
  hub.enc_stall_s = 0.0
  return hub


@pytest.fixture
def reboots(monkeypatch):
  calls = []

  class FakeTimer:
    def __init__(self, delay, fn):
      calls.append(fn)

    def start(self):
      pass

  monkeypatch.setattr(starviewd.threading, "Timer", FakeTimer)
  return calls


@pytest.mark.parametrize("onroad,started", [(True, False), (False, True), (True, True)])
def test_reboot_refused_while_car_is_on(reboots, onroad, started):
  hub = make_hub(onroad=onroad, started=started)
  ack = hub.control({"action": "reboot"})
  assert ack["ok"] is False
  assert reboots == []
  assert "DoUserReboot" not in hub.params.values


def test_reboot_allowed_offroad(reboots):
  hub = make_hub()
  ack = hub.control({"action": "reboot"})
  assert ack["ok"] is True
  assert len(reboots) == 1


@pytest.mark.parametrize("engaged,moving", [(True, False), (False, True), (True, True)])
def test_drive_state_refused_while_engaged_or_moving(monkeypatch, engaged, moving):
  hub = make_hub(onroad=True, started=True, engaged=engaged)
  monkeypatch.setattr(hub, "_moving", lambda: moving)
  ack = hub.control({"action": "drive_state", "mode": "offroad"})
  assert ack["ok"] is False
  assert "ForceOffroad" not in hub.params.values


def test_drive_state_allowed_when_stopped_and_disengaged(monkeypatch):
  hub = make_hub(onroad=True, started=True)
  monkeypatch.setattr(hub, "_moving", lambda: False)
  ack = hub.control({"action": "drive_state", "mode": "offroad"})
  assert ack["ok"] is True
  assert hub.params.values["ForceOffroad"] is True


def test_not_moving_without_reading_carstate_when_offroad():
  hub = make_hub()
  assert hub._moving() is False
  assert hub._car_state_sock is None


def test_params_snapshot_is_shared_between_tablets(monkeypatch):
  hub = make_hub()
  now = [100.0]
  monkeypatch.setattr(starviewd.time, "monotonic", lambda: now[0])
  first = hub.params_snapshot()
  reads = hub.params.reads
  assert hub.params_snapshot() is first  # a second tablet in the same second: no new reads
  assert hub.params.reads == reads

  hub.invalidate_params_snapshot()  # after a control action the next snapshot is fresh
  assert hub.params_snapshot() is not first

  now[0] += 1.0
  third = hub.params_snapshot()
  assert hub.params.reads > reads
  assert third is not first
