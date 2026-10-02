from collections import deque

from openpilot.starpilot.system import speed_limit_filler as slf


class FakeParams:
  def __init__(self, values=None):
    self.values = dict(values or {})
    self.reads = 0

  def get(self, key):
    self.reads += 1
    return self.values.get(key)

  def put(self, key, value):
    self.values[key] = value


def entry(i, speed=25.0):
  return {
    "bearing": float(i % 360),
    "end_coordinates": {"latitude": 1.0 + i, "longitude": 2.0},
    "incorrect_limit": False,
    "road_name": f"Road {i % 3}",
    "road_width": 3.5,
    "source": "Dashboard",
    "speed_limit": speed,
    "start_coordinates": {"latitude": 1.0, "longitude": 2.0},
  }


def make_logger(existing):
  logger = object.__new__(slf.MapSpeedLogger)
  logger.params = FakeParams({"SpeedLimits": list(existing)})
  logger.dataset_additions = deque(maxlen=slf.MAX_PENDING_ADDITIONS)
  logger._cleaned_dataset = None
  logger.last_dataset_flush = float("-inf")
  return logger


def flush_due(logger):
  # Pretend the periodic flush interval has elapsed.
  logger.last_dataset_flush = float("-inf")
  logger.flush_pending_dataset_additions(force=False)


def test_incremental_flushes_match_full_cleanup(monkeypatch):
  existing = [entry(i) for i in range(20)] + [entry(3), {"invalid": True}]
  batches = [[entry(i) for i in range(15, 30)], [entry(5, speed=30.0), entry(40)], [entry(41), entry(15)]]

  logger = make_logger(existing)
  expected = list(existing)
  for batch in batches:
    logger.dataset_additions.extend(batch)
    flush_due(logger)
    expected.extend(batch)
    assert logger.params.values["SpeedLimits"] == list(slf.MapSpeedLogger.cleanup_dataset(expected))

  # The stored dataset is only read once per drive.
  assert logger.params.reads == 1


def test_forced_flush_rereads_dataset_next_drive():
  logger = make_logger([entry(1)])
  logger.dataset_additions.append(entry(2))
  logger.flush_pending_dataset_additions(force=True)
  assert logger._cleaned_dataset is None

  # Offroad processing rewrites the dataset; the next drive must start from that.
  logger.params.values["SpeedLimits"] = [entry(7)]
  logger.dataset_additions.append(entry(8))
  flush_due(logger)
  assert logger.params.values["SpeedLimits"] == [entry(7), entry(8)]

  # A forced flush with nothing pending also drops the cache.
  logger.flush_pending_dataset_additions(force=True)
  assert logger._cleaned_dataset is None


def test_max_entries_keeps_newest(monkeypatch):
  monkeypatch.setattr(slf, "MAX_ENTRIES", 5)
  logger = make_logger([entry(i) for i in range(4)])
  logger.dataset_additions.extend(entry(i) for i in range(4, 8))
  flush_due(logger)
  assert logger.params.values["SpeedLimits"] == [entry(i) for i in range(3, 8)]
