from openpilot.starpilot.common.vision_bsm import VASM_STATE_TIMEOUT_SECONDS, get_fresh_vasm_state


class FakeParams:
  def __init__(self, values):
    self.values = values

  def get(self, key):
    return self.values.get(key)


def test_fresh_vasm_state_is_returned():
  params = FakeParams({
    "VASMLastUpdateMonoTime": "100.0",
    "VASMLeftActive": "1",
    "VASMRightActive": "0",
  })

  assert get_fresh_vasm_state(params, now=101.0) == (True, False)


def test_stale_or_invalid_vasm_state_fails_closed():
  stale = FakeParams({"VASMLastUpdateMonoTime": "100.0", "VASMLeftActive": "1"})
  invalid = FakeParams({"VASMLastUpdateMonoTime": "invalid", "VASMLeftActive": "1"})

  assert get_fresh_vasm_state(stale, now=100.0 + VASM_STATE_TIMEOUT_SECONDS + 0.01) == (False, False)
  assert get_fresh_vasm_state(invalid, now=100.0) == (False, False)


def test_cached_vasm_state_shares_reads_within_max_age(monkeypatch):
  from openpilot.starpilot.common import vision_bsm

  class CountingParams(FakeParams):
    reads = 0

    def get(self, key):
      self.reads += 1
      return super().get(key)

  now = [100.5]
  monkeypatch.setattr(vision_bsm.time, "monotonic", lambda: now[0])
  monkeypatch.setattr(vision_bsm, "_cached_vasm_state", (float("-inf"), (False, False)))
  params = CountingParams({"VASMLastUpdateMonoTime": "100.0", "VASMLeftActive": "1", "VASMRightActive": "0"})

  assert vision_bsm.get_fresh_vasm_state_cached(params) == (True, False)
  reads = params.reads
  assert vision_bsm.get_fresh_vasm_state_cached(params) == (True, False)
  assert params.reads == reads

  params.values["VASMRightActive"] = "1"
  now[0] += 0.06
  assert vision_bsm.get_fresh_vasm_state_cached(params) == (True, True)
