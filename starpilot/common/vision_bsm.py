from __future__ import annotations

import time


VASM_STATE_TIMEOUT_SECONDS = 3.0


def get_fresh_vasm_state(params_memory, now: float | None = None) -> tuple[bool, bool]:
  """Return V-ASM state only while the vision daemon is updating it."""
  try:
    updated_at = float(params_memory.get("VASMLastUpdateMonoTime") or 0)
  except (TypeError, ValueError):
    return False, False

  current_time = time.monotonic() if now is None else now
  age = current_time - updated_at
  if updated_at <= 0 or age < 0 or age > VASM_STATE_TIMEOUT_SECONDS:
    return False, False

  active_values = ("1", b"1", True)
  return params_memory.get("VASMLeftActive") in active_values, params_memory.get("VASMRightActive") in active_values


_cached_vasm_state: tuple[float, tuple[bool, bool]] = (float("-inf"), (False, False))


def get_fresh_vasm_state_cached(params_memory, max_age: float = 0.05) -> tuple[bool, bool]:
  """Like get_fresh_vasm_state, but shares one read across callers for max_age seconds.

  The UI asks for this from several widgets every frame; each uncached call is up to three param file reads.
  """
  global _cached_vasm_state
  now = time.monotonic()
  read_at, state = _cached_vasm_state
  if now - read_at >= max_age:
    state = get_fresh_vasm_state(params_memory, now)
    _cached_vasm_state = (now, state)
  return state
