"""StarView tablet mode: while a StarView tablet shows the drive, the comma's own screen stays off and the UI loop
idles at 5 fps. starviewd refreshes /dev/shm/starview_tablet once a second while a tablet is connected; a flag older
than FLAG_MAX_AGE_S means the tablet is gone and the screen comes back. A critical alert always brings it back.
"""
import os
import time

from cereal import log

FLAG_PATH = "/dev/shm/starview_tablet"
FLAG_MAX_AGE_S = 5.0
TABLET_FPS = 5
CRITICAL = log.SelfdriveState.AlertStatus.critical


class StarViewTabletMode:
  def __init__(self, gui_app, device, ui_state, flag_path: str = FLAG_PATH):
    self._gui_app = gui_app
    self._device = device
    self._ui_state = ui_state
    self._flag_path = flag_path
    self._active = False
    self._flag_fresh = False
    self._check_t = float("-inf")
    self._saved_fps = (gui_app._full_target_fps, gui_app._idle_target_fps)

    # keep the screen off while active, even when the normal wake logic (touch, ignition) would turn it on
    self._orig_update_wakefulness = device._update_wakefulness
    device._update_wakefulness = self._update_wakefulness

  @property
  def active(self) -> bool:
    return self._active

  def _update_wakefulness(self):
    if self._active:
      if self._device._awake:
        self._device._set_awake(False)
    else:
      self._orig_update_wakefulness()

  def _flag_is_fresh(self) -> bool:
    try:
      # file mtimes are wall-clock time
      return time.time() - os.path.getmtime(self._flag_path) < FLAG_MAX_AGE_S  # noqa: TID251
    except OSError:
      return False

  def _critical_alert(self) -> bool:
    try:
      sm = self._ui_state.sm
      return bool(sm.recv_frame["selfdriveState"]) and sm["selfdriveState"].alertStatus == CRITICAL
    except Exception:
      return False

  def update(self, now: float) -> None:
    if now - self._check_t >= 1.0:
      self._check_t = now
      self._flag_fresh = self._flag_is_fresh()
    active = self._ui_state.started and self._flag_fresh and not self._critical_alert()
    if active == self._active:
      return

    self._active = active
    app = self._gui_app
    if active:
      # idle the whole loop: both fps targets, so the adaptive scheduler can't raise it again
      self._saved_fps = (app._full_target_fps, app._idle_target_fps)
      app._full_target_fps = app._idle_target_fps = TABLET_FPS
      app._set_target_fps(TABLET_FPS)
      self._device._set_awake(False)
    else:
      app._full_target_fps, app._idle_target_fps = self._saved_fps
      app._set_target_fps(app._full_target_fps)
      self._device.reset_interactive_timeout()
      self._device._set_awake(True)
