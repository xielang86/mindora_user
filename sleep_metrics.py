"""Home/Day metrics: observed inBed first, explicit conservative estimates otherwise.

All intervals use Unix seconds internally; SleepElement.duration is minutes.
The existing sequence_summaries.time_in_bed is stage duration, not bed occupancy.
"""
import datetime
import math
import statistics
from typing import Optional

from user_profile import SleepResult, UserProfile

MAX_SPAN = 16 * 3600
MAX_GAP = 3 * 3600
MAX_ONSET = 180
DEFAULT_ONSET = 15  # product prior, not an individual measurement
DEEP_FIRST_ONSET = 10  # existing product fallback


def _union(intervals):
  out = []
  for start, end in sorted(intervals):
    if not (math.isfinite(start) and math.isfinite(end)) or start <= 0 or not 0 < end-start <= MAX_SPAN:
      continue
    if out and start <= out[-1][1]:
      out[-1] = (out[-1][0], max(out[-1][1], end))
    else:
      out.append((start, end))
  return out


def _timeline(record):
  rows = [(float(x.start_time), x.start_time+x.duration*60, x.sleep_type) for x in record.sleep_status
          if x.sleep_type in {"awake", "core", "deep", "rem"} and math.isfinite(x.duration)
          and 0 < x.duration*60 <= MAX_SPAN and x.start_time > 0]
  # Split overlaps; awake wins conflicting samples, duplicate sleep never counts twice.
  points = sorted({p for a,b,_ in rows for p in (a,b)})
  out = []
  for a,b in zip(points, points[1:]):
    active = [t for s,e,t in rows if s < b and e > a]
    if active:
      out.append((a,b,"awake" if "awake" in active else active[0]))
  return out


def _confirmed_start(rows):
  start = None
  asleep = 0
  last_end = None
  awake_start = None
  for a,b,t in rows:
    if last_end is not None and a-last_end > 60:
      start, asleep, awake_start = None, 0, None
    if t == "awake":
      awake_start = a if awake_start is None else awake_start
      if b-awake_start > 60:
        start, asleep = None, 0
    else:
      if awake_start is not None and a-awake_start > 60:
        start, asleep = None, 0
      awake_start = None
      if start is None:
        start = a
      asleep += b-a
      if asleep >= 300:
        return start  # confirm five minutes, return the beginning of the sustained bout
    last_end = b
  return None


def _bed_intervals(record, behaviors, rows):
  candidates = list(record.in_bed_intervals)
  for entry in (behaviors or {}).get("sleep_in_bed", []) or []:
    if not isinstance(entry, (list, tuple)) or len(entry) < 2:
      continue
    try:
      a, duration = float(entry[0]), float(entry[1])
      candidates.append((a, a+duration))
    except (ValueError, TypeError, OverflowError):
      continue
  if not rows:
    return []  # cannot reliably associate raw inBed samples with a scored-only row
  first, last = rows[0][0], rows[-1][1]
  beds = _union([(a,b) for a,b in candidates if a < last and b > first])
  if not beds or beds[-1][1]-beds[0][0] > MAX_SPAN:
    return []
  # Partial/other-session inBed must not be called a measured whole-night value.
  if any(not any(a <= s and b >= e for a,b in beds) for s,e,_ in rows):
    return []
  return beds


def _valid_onset(value):
  return value is not None and math.isfinite(value) and 0 <= value <= MAX_ONSET


def _personal_onset(profile, current, behaviors, tz):
  values = []
  dates = set()
  for old in sorted(profile.sleep_data, key=lambda r: r.timestamp, reverse=True):
    if not 0 < current.timestamp-old.timestamp <= 30*86400:
      continue
    rows = _timeline(old)
    beds = _bed_intervals(old, behaviors, rows)
    start = _confirmed_start(rows)
    if beds and start is not None:
      latency = (start-beds[0][0])/60
      # Only real inBed-derived samples enter the prior; never feed estimates back.
      day = datetime.datetime.fromtimestamp(old.timestamp, tz).date()
      if _valid_onset(latency) and day not in dates:
        values.append(latency)
        dates.add(day)
    if len(values) == 14:
      break
  return max(1, min(60, statistics.median(values))) if len(values) >= 3 else None


def build_sleep_metrics(record: Optional[SleepResult], profile: Optional[UserProfile], tz) -> Optional[dict]:
  if record is None:
    return None
  result = {"sleep_onset_minutes": None, "time_in_bed_minutes": None,
            "source": "estimated", "date": datetime.datetime.fromtimestamp(record.timestamp,tz).date().isoformat()}
  rows = _timeline(record)
  if not rows:
    if _valid_onset(record.onset):
      result["sleep_onset_minutes"] = int(record.onset)
    return result
  if rows[-1][1]-rows[0][0] > MAX_SPAN or any(b[0]-a[1] > MAX_GAP for a,b in zip(rows,rows[1:])):
    return result
  behaviors = profile.behaviors if profile else {}
  beds = _bed_intervals(record, behaviors, rows)
  sleep_start = _confirmed_start(rows)
  if beds:
    result["source"] = "measured"
    result["time_in_bed_minutes"] = math.ceil(sum(b-a for a,b in beds)/60)
    if sleep_start is not None:
      latency = (sleep_start-beds[0][0])/60
      if _valid_onset(latency):
        result["sleep_onset_minutes"] = int(latency)
    return result
  first, last = rows[0][0], rows[-1][1]
  bed_start = first
  latency = None
  if sleep_start is not None:
    if rows[0][2] == "awake":
      latency = (sleep_start-first)/60
    elif record.onset is not None:
      # Explicit outliers remain unknown rather than silently replacing them with a prior.
      latency = record.onset
    else:
      latency = _personal_onset(profile, record, behaviors, tz) if profile else None
      if latency is None:
        latency = DEEP_FIRST_ONSET if rows[0][2] == "deep" else DEFAULT_ONSET
    if _valid_onset(latency):
      result["sleep_onset_minutes"] = int(latency)
      bed_start = min(first, sleep_start-latency*60)
  duration = (last-bed_start)/60
  # Avoid reporting total sleep as time in bed when neither boundary nor extra time is known.
  asleep = sum(b-a for a,b,t in rows if t != "awake")/60
  if duration*60 <= MAX_SPAN and (duration > asleep or result["sleep_onset_minutes"] is not None):
    result["time_in_bed_minutes"] = math.ceil(duration)
  return result
