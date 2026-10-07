import time

from openpilot.common.swaglog import cloudlog
from openpilot.selfdrive.ui.mici.layouts.settings.galaxy import GalaxyQRDialog
from openpilot.selfdrive.ui.mici.widgets.button import BigButton
from openpilot.selfdrive.ui.mici.widgets.dialog import BigDialog, BigConfirmationDialog, BigMultiOptionDialog
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.starpilot.system.starview import pairing
from openpilot.system.ui.lib.application import FontWeight, gui_app
from openpilot.system.ui.widgets.label import UnifiedLabel

SHOW_QR = "show qr"
UNPAIR = "unpair tablets"
USB_KEY_ON = "usb needs key: on"
USB_KEY_OFF = "usb needs key: off"


class StarViewQRDialog(GalaxyQRDialog):
  """The StarView app scans this once; it holds the comma's addresses and the pairing key."""

  def __init__(self, uri: str):
    super().__init__(uri)
    self._title = UnifiedLabel("scan with starview", font_size=48, font_weight=FontWeight.BOLD, line_height=0.8)


class StarViewBigButton(BigButton):
  def __init__(self):
    super().__init__("starview", "", gui_app.texture("icons_mici/settings/device_icon.png", 64, 64))
    self._usb_key_checked_at = float("-inf")

  def _get_label_font_size(self):
    return 64

  def _show_qr(self):
    try:
      uri = pairing.pairing_uri(ui_state.params.get("DongleId") or "")
    except Exception as e:
      cloudlog.warning(f"StarView pairing QR failed: {e}")
      gui_app.push_widget(BigDialog("", "Could not create the StarView pairing code."))
      return
    gui_app.push_widget(StarViewQRDialog(uri))

  def _unpair(self):
    try:
      pairing.rotate_key()
    except Exception as e:
      cloudlog.warning(f"StarView unpair failed: {e}")
      gui_app.push_widget(BigDialog("", "Could not unpair StarView tablets."))

  def _toggle_usb_key(self):
    try:
      pairing.set_usb_requires_key(not pairing.usb_requires_key())
    except Exception as e:
      cloudlog.warning(f"StarView usb key switch failed: {e}")
    self._usb_key_checked_at = float("-inf")

  def _handle_mouse_release(self, mouse_pos):
    super()._handle_mouse_release(mouse_pos)

    usb_option = USB_KEY_OFF if pairing.usb_requires_key() else USB_KEY_ON
    dialog_holder: dict[str, BigMultiOptionDialog] = {}

    def on_confirm():
      selection = dialog_holder["dialog"].get_selected_option()
      if selection == SHOW_QR:
        self._show_qr()
      elif selection == UNPAIR:
        gui_app.push_widget(
          BigConfirmationDialog(
            "slide to unpair\nstarview tablets",
            gui_app.texture("icons_mici/settings/device/uninstall.png", 64, 64),
            self._unpair,
            red=True,
          )
        )
      elif selection == usb_option:
        self._toggle_usb_key()

    dialog = BigMultiOptionDialog(options=[SHOW_QR, UNPAIR, usb_option], default=SHOW_QR, right_btn_callback=on_confirm)
    dialog_holder["dialog"] = dialog
    gui_app.push_widget(dialog)

  def _update_state(self):
    # runs every frame: look at the flag file at most once a second
    now = time.monotonic()
    if now - self._usb_key_checked_at >= 1.0:
      self._usb_key_checked_at = now
      self.set_value("qr only" if pairing.usb_requires_key() else "pair")
