import os
import time
from types import SimpleNamespace

from cereal import log
from openpilot.selfdrive.ui.starview_tablet_mode import CRITICAL, TABLET_FPS, StarViewTabletMode


class FakeApp:
  def __init__(self):
    self._full_target_fps = 60
    self._idle_target_fps = 30
    self.target = 60

  def _set_target_fps(self, fps):
    self.target = fps


class FakeDevice:
  def __init__(self):
    self._awake = True
    self.wake_calls = 0

  def _update_wakefulness(self):
    self.wake_calls += 1
    self._set_awake(True)

  def _set_awake(self, on):
    self._awake = on

  def reset_interactive_timeout(self):
    pass


class FakeSM(dict):
  def __init__(self, alert_status):
    super().__init__(selfdriveState=SimpleNamespace(alertStatus=alert_status))
    self.recv_frame = {"selfdriveState": 1}


def make(tmp_path, started=True, alert=log.SelfdriveState.AlertStatus.normal):
  app, dev = FakeApp(), FakeDevice()
  ui_state = SimpleNamespace(started=started, sm=FakeSM(alert))
  flag = tmp_path / "starview_tablet"
  mode = StarViewTabletMode(app, dev, ui_state, flag_path=str(flag))
  return mode, app, dev, ui_state, flag


def test_screen_off_while_tablet_connected_and_back_when_it_leaves(tmp_path):
  mode, app, dev, _, flag = make(tmp_path)
  flag.write_text("1")
  mode.update(10.0)
  assert mode.active and not dev._awake and app.target == TABLET_FPS

  dev._update_wakefulness()  # normal wake logic must not turn the screen on while active
  assert not dev._awake and dev.wake_calls == 0

  old = time.time() - 10  # noqa: TID251 file mtimes are wall-clock time
  os.utime(flag, (old, old))  # tablet stopped refreshing the flag
  mode.update(11.0)
  assert not mode.active and dev._awake
  assert (app._full_target_fps, app._idle_target_fps, app.target) == (60, 30, 60)
  dev._update_wakefulness()
  assert dev.wake_calls == 1


def test_critical_alert_always_lights_the_comma_screen(tmp_path):
  mode, _, dev, ui_state, flag = make(tmp_path)
  flag.write_text("1")
  mode.update(10.0)
  assert mode.active

  ui_state.sm = FakeSM(CRITICAL)
  mode.update(10.1)  # checked every frame, not once a second
  assert not mode.active and dev._awake


def test_offroad_never_enters_tablet_mode(tmp_path):
  mode, _, dev, _, flag = make(tmp_path, started=False)
  flag.write_text("1")
  mode.update(10.0)
  assert not mode.active and dev._awake
