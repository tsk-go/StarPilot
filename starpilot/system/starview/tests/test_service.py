import subprocess

import pytest

from openpilot.starpilot.system.starview import service


class FakeSystemd:
  """Stands in for sudo/systemctl/systemd-run: records commands, keeps one unit's state."""
  def __init__(self):
    self.active = False
    self.calls = []
    self.run_ok = True

  def __call__(self, *cmd, timeout=20.0):
    self.calls.append(cmd)
    rc = 0
    if cmd[:3] == ("sudo", "-n", "systemctl"):
      verb = cmd[3]
      if verb == "is-active":
        rc = 0 if self.active else 3
      elif verb == "stop":
        self.active = False
    elif cmd[:3] == ("sudo", "-n", "systemd-run"):
      if self.run_ok and not self.active:
        self.active = True
      else:
        rc = 1
    return subprocess.CompletedProcess(cmd, rc, "", "" if rc == 0 else "Unit starview.service already exists")

  def verbs(self):
    return [c[3] if c[2] == "systemctl" else c[2] for c in self.calls]


@pytest.fixture
def systemd(tmp_path, monkeypatch):
  fake = FakeSystemd()
  monkeypatch.setattr(service, "_run", fake)
  monkeypatch.setattr(service, "VERSION_FILE", tmp_path / "version")
  monkeypatch.setattr(service, "DISABLE_FLAG", tmp_path / "disabled")
  monkeypatch.setattr(service, "code_version", lambda: "v1")
  return fake


def test_starts_the_unit_when_it_is_not_running(systemd):
  service.ensure()
  assert systemd.active
  assert "systemd-run" in systemd.verbs()
  assert service.started_version() == "v1"


def test_leaves_a_running_unit_alone(systemd):
  service.ensure()
  systemd.calls.clear()
  for _ in range(5):
    service.ensure()
  assert systemd.verbs() == ["is-active"] * 5  # one cheap check per tick, never a restart


def test_restarts_it_after_an_update(systemd, monkeypatch):
  service.ensure()
  monkeypatch.setattr(service, "code_version", lambda: "v2")
  systemd.calls.clear()
  service.ensure()
  assert systemd.verbs()[:2] == ["is-active", "stop"]
  assert "systemd-run" in systemd.verbs()
  assert service.started_version() == "v2"


def test_starts_it_again_if_it_stopped(systemd):
  service.ensure()
  systemd.active = False  # crashed past its own restarts, or someone stopped it
  service.ensure()
  assert systemd.active


def test_disable_flag_stops_it_and_keeps_it_stopped(systemd):
  service.ensure()
  service.DISABLE_FLAG.touch()
  systemd.calls.clear()
  service.ensure()
  assert not systemd.active
  service.ensure()
  assert not systemd.active
  assert "systemd-run" not in systemd.verbs()


def test_failed_start_is_retried_next_tick(systemd):
  systemd.run_ok = False
  service.ensure()
  assert not systemd.active and service.started_version() == ""
  systemd.run_ok = True
  service.ensure()
  assert systemd.active


def test_start_command_runs_starviewd_as_us_with_our_environment():
  env = {"PYTHONPATH": "/data/openpilot", "PATH": "/usr/local/venv/bin:/usr/bin", "INVOCATION_ID": "abc",
         "NOTIFY_SOCKET": "/run/systemd/notify", "WEIRD": "a\nb"}
  cmd = service.start_command(env)
  assert cmd[:3] == ["sudo", "-n", "systemd-run"]
  assert "--unit=starview" in cmd
  assert any(c.startswith("--uid=") for c in cmd) and any(c.startswith("--gid=") for c in cmd)
  assert "Restart=always" in cmd
  assert "--setenv=PYTHONPATH=/data/openpilot" in cmd
  assert not any("INVOCATION_ID" in c or "NOTIFY_SOCKET" in c or "WEIRD" in c for c in cmd)
  assert cmd[-2:] == ["-m", "openpilot.starpilot.system.starview.starviewd"]


def test_code_version_changes_with_the_code(tmp_path, monkeypatch):
  (tmp_path / "starviewd.py").write_text("a = 1\n")
  monkeypatch.setattr(service, "CODE_DIR", tmp_path)
  v1 = service.code_version()
  assert service.code_version() == v1
  (tmp_path / "starviewd.py").write_text("a = 2\n")
  assert service.code_version() != v1


def test_runs_starviewd_in_the_manager_without_systemd(monkeypatch):
  from openpilot.starpilot.system.starview import starviewd
  ran = []
  monkeypatch.setattr(service, "systemd_usable", lambda: False)
  monkeypatch.setattr(starviewd, "main", lambda: ran.append(True))
  service.main()
  assert ran == [True]


def test_falls_back_to_the_manager_if_the_unit_never_starts(systemd, monkeypatch):
  """Never worse than before: if this systemd refuses the unit, StarView still runs (inside openpilot)."""
  ran = []
  systemd.run_ok = False
  monkeypatch.setattr(service, "systemd_usable", lambda: True)
  monkeypatch.setattr(service, "run_inside_manager", lambda: ran.append(True))
  monkeypatch.setattr(service.time, "sleep", lambda s: None)
  service.main()
  assert ran == [True]
  assert systemd.verbs().count("systemd-run") == service.MAX_START_FAILURES


def test_no_systemd_on_pc():
  # this test machine is a PC: the manager runs starviewd directly, exactly like before
  assert service.systemd_usable() is False
