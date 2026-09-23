"""Home/Day metrics: observed inBed first, explicit conservative estimates otherwise.

All intervals use Unix seconds internally; SleepElement.duration is minutes.
Sleep onset is owned by sleep_session_builder; this module estimates bed occupancy.
"""
import datetime
import math
import statistics
from typing import Optional

from user_profile import SleepResult, UserProfile
from sleep_session_builder import resolve_sleep_onset

MAX_SPAN = 16 * 3600
MAX_GAP = 3 * 3600
DEFAULT_ONSET = 15  # product prior, not an individual measurement


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


def _personal_onset(profile, current, behaviors, tz):
  values = []
  dates = set()
  for old in sorted(profile.sleep_data, key=lambda r: r.timestamp, reverse=True):
    if not 0 < current.timestamp-old.timestamp <= 30*86400:
      continue
    rows = _timeline(old)
    beds = _bed_intervals(old, behaviors, rows)
    # 仅用于卧床起点估计，不作为另一个对外 onset；历史样本也复用合成算法。
    if beds:
      measured = old.model_copy(update={"onset": None, "in_bed_intervals": beds})
      latency = resolve_sleep_onset(measured)
      day = datetime.datetime.fromtimestamp(old.timestamp, tz).date()
      if latency is not None and day not in dates:
        values.append(latency)
        dates.add(day)
    if len(values) == 14:
      break
  return max(1, min(60, statistics.median(values))) if len(values) >= 3 else None


def build_sleep_metrics(record: Optional[SleepResult], profile: Optional[UserProfile], tz) -> Optional[dict]:
  if record is None:
    return None
  onset = resolve_sleep_onset(record)
  result = {"sleep_onset_minutes": int(onset) if onset is not None else None,
            "time_in_bed_minutes": None, "source": "estimated",
            "date": datetime.datetime.fromtimestamp(record.timestamp,tz).date().isoformat()}
  rows = _timeline(record)
  if not rows:
    return result
  if rows[-1][1]-rows[0][0] > MAX_SPAN or any(b[0]-a[1] > MAX_GAP for a,b in zip(rows,rows[1:])):
    return result
  behaviors = profile.behaviors if profile else {}
  beds = _bed_intervals(record, behaviors, rows)
  if beds:
    result["source"] = "measured"
    result["time_in_bed_minutes"] = math.ceil(sum(b-a for a,b in beds)/60)
    return result
  first, last = rows[0][0], rows[-1][1]
  bed_start = first
  if rows[0][2] != "awake":
    latency = onset
    if latency is None and record.onset is None:
      # 仅用于估算卧床时长；缺失的入睡用时保持 null，与 Explore/文案一致。
      latency = _personal_onset(profile, record, behaviors, tz) if profile else None
      if latency is None:
        latency = DEFAULT_ONSET
    if latency is not None:
      bed_start -= latency * 60
  duration = (last-bed_start)/60
  asleep = sum(b-a for a,b,t in rows if t != "awake")/60
  if duration*60 <= MAX_SPAN and (duration > asleep or onset is not None):
    result["time_in_bed_minutes"] = math.ceil(duration)
  return result
