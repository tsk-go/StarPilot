#!/usr/bin/env python3
"""Runs the StarView bridge (starviewd) as its own small system service, outside openpilot.

The manager starts this module like any other process. It hands starviewd to systemd as a separate unit,
starview.service, so the bridge, the tablet's terminal and file copying keep running while openpilot is stopped
(`sudo systemctl stop comma`) or restarting. From then on it only watches, every CHECK_S seconds:
  * the unit stopped           -> start it again
  * the StarView code changed  -> restart it, so an update takes effect
  * /data/starview/disabled    -> stop it and leave it stopped
The unit is transient: it's gone after a reboot, and the manager starts it again at boot.

Where systemd can't be used (a PC, no passwordless sudo, or the unit fails to start MAX_START_FAILURES times in a row)
it runs starviewd itself, inside the manager, exactly as before.
"""
import hashlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from openpilot.common.basedir import BASEDIR
from openpilot.common.swaglog import cloudlog

UNIT = "starview"
CHECK_S = 10.0
MAX_START_FAILURES = 3
CODE_DIR = Path(__file__).resolve().parent
STATE_DIR = Path(os.getenv("STARVIEW_SERVICE_STATE", "/dev/shm"))  # cleared on reboot, like the transient unit
VERSION_FILE = STATE_DIR / "starview_service_version"
DISABLE_FLAG = Path("/data/starview/disabled")
MODULE = "openpilot.starpilot.system.starview.starviewd"
# set by systemd for the manager itself; must not leak into the new unit
SKIP_ENV = {"INVOCATION_ID", "JOURNAL_STREAM", "NOTIFY_SOCKET", "LISTEN_PID", "LISTEN_FDS", "LISTEN_FDNAMES",
            "SYSTEMD_EXEC_PID", "WATCHDOG_PID", "WATCHDOG_USEC", "MANAGERPID", "_"}


def code_version() -> str:
  """Changes whenever a StarView source file changes (git pull / update)."""
  h = hashlib.sha256()
  for f in sorted(CODE_DIR.glob("*.py")):
    h.update(f.name.encode())
    h.update(f.read_bytes())
  return h.hexdigest()[:16]


def _run(*cmd: str, timeout: float = 20.0) -> subprocess.CompletedProcess:
  return subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout)


def _systemctl(*args: str) -> subprocess.CompletedProcess:
  return _run("sudo", "-n", "systemctl", *args)


def systemd_usable() -> bool:
  try:
    from openpilot.system.hardware import PC
  except Exception:
    PC = True
  if PC or not Path("/run/systemd/system").is_dir() or shutil.which("systemd-run") is None:
    return False
  try:
    return _run("sudo", "-n", "true", timeout=5).returncode == 0
  except Exception:
    return False


def unit_active() -> bool:
  return _systemctl("is-active", "--quiet", UNIT).returncode == 0


def started_version() -> str:
  try:
    return VERSION_FILE.read_text().strip()
  except OSError:
    return ""


def start_command(env: dict[str, str] | None = None) -> list[str]:
  env = dict(os.environ if env is None else env)
  setenv = [f"--setenv={k}={v}" for k, v in sorted(env.items()) if k not in SKIP_ENV and "\n" not in v and "=" not in k]
  return ["sudo", "-n", "systemd-run", f"--unit={UNIT}", "--collect", "--description=StarView tablet bridge",
          f"--uid={os.getuid()}", f"--gid={os.getgid()}",
          "-p", "Restart=always", "-p", "RestartSec=3", "-p", "Nice=5",
          "-p", f"WorkingDirectory={BASEDIR}", *setenv, sys.executable, "-m", MODULE]


def stop_unit() -> None:
  _systemctl("stop", UNIT)
  _systemctl("reset-failed", UNIT)


def start_unit(version: str) -> bool:
  _systemctl("reset-failed", UNIT)  # a failed run that wasn't collected yet would block the name
  r = _run(*start_command())
  if r.returncode != 0:
    cloudlog.warning(f"starview service: systemd-run failed: {r.stderr.strip()[:300]}")
    return False
  try:
    VERSION_FILE.write_text(version)
  except OSError:
    pass
  cloudlog.info(f"starview service: started {UNIT}.service (code {version})")
  return True


def ensure() -> bool:
  """One check: get the unit into the state it should be in. False: it should run but couldn't be started."""
  if DISABLE_FLAG.exists():
    if unit_active():
      cloudlog.info("starview service: disabled by /data/starview/disabled, stopping it")
      stop_unit()
    return True
  want = code_version()
  if unit_active():
    if started_version() == want:
      return True
    cloudlog.info("starview service: StarView code changed, restarting it")
    stop_unit()
  return start_unit(want)


def run_inside_manager() -> None:
  from openpilot.starpilot.system.starview import starviewd
  starviewd.main()


def main() -> None:
  if not systemd_usable():
    cloudlog.info("starview service: no systemd/sudo here, running starviewd inside the manager")
    run_inside_manager()
    return
  failures = 0
  while True:
    try:
      ok = ensure()
    except Exception as e:
      cloudlog.warning(f"starview service: {e}")
      ok = False
    failures = 0 if ok else failures + 1
    if failures >= MAX_START_FAILURES and not unit_active():
      cloudlog.error("starview service: can't start starview.service, running starviewd inside the manager instead")
      run_inside_manager()
      return
    time.sleep(CHECK_S)


if __name__ == "__main__":
  main()
