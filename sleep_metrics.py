"""Home/Day metrics: observed inBed first, explicit conservative estimates otherwise.

All intervals use Unix seconds internally; SleepElement.duration is minutes.
Sleep onset is owned by sleep_session_builder; this module estimates bed occupancy.
"""
import datetime
import math
from typing import Optional

from user_profile import SleepResult, UserProfile
from sleep_session_builder import resolve_sleep_onset

MAX_SPAN = 16 * 3600
MAX_GAP = 3 * 3600
DEFAULT_ONSET = 15  # product prior, not an individual measurement
MAX_PLAY_TO_SLEEP = 2 * 3600  # older playback is not evidence of bedtime


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
          if x.sleep_type in {"awake", "core", "deep", "rem", "unspecified"} and math.isfinite(x.duration)
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


def _nearest_mindora_start(profile, first_sleep):
  if not profile:
    return None
  latest = None
  for scene_id, entries in (profile.mindora_record or {}).items():
    if not scene_id.startswith(("sleep.scene.", "sleep.pure_music.")):
      continue
    for entry in entries or []:
      try:
        started = int(entry[0])
      except (IndexError, TypeError, ValueError, OverflowError):
        continue
      if 0 < first_sleep - started <= MAX_PLAY_TO_SLEEP and (latest is None or started > latest):
        latest = started
  return latest


def _first_effective_sleep(rows):
  """First five minutes of sleep; brief (<=1 min) awake does not reset it."""
  accumulated = 0.0
  candidate = None
  for start, end, kind in rows:
    if kind == "awake":
      if end - start > 60:
        accumulated = 0.0
        candidate = None
      continue
    if candidate is None:
      candidate = start
    accumulated += end - start
    if accumulated >= 300:
      return candidate
  return None


def estimate_sleep_window(record: SleepResult, profile: Optional[UserProfile]) -> Optional[dict]:
  """Resolve first sleep, bed start and TIB with explicit provenance."""
  rows = _timeline(record)
  first_stage = next((a for a, _, kind in rows if kind != "awake"), None)
  first_sleep = _first_effective_sleep(rows)
  if first_sleep is None or not rows or rows[-1][1] - rows[0][0] > MAX_SPAN:
    return None
  if any(b[0] - a[1] > MAX_GAP for a, b in zip(rows, rows[1:])):
    return None
  last = rows[-1][1]
  beds = _bed_intervals(record, profile.behaviors if profile else {}, rows)
  if beds and beds[0][0] < first_stage:
    bed_start = beds[0][0]
    tib = sum(b - a for a, b in beds) / 60
    source = "measured"
    onset_source = "in_bed"
    latency = (first_sleep - bed_start) / 60
  else:
    pre_awake = [a for a, b, kind in rows if kind == "awake" and a < first_stage and b <= first_stage]
    play_start = _nearest_mindora_start(profile, first_sleep)
    reported = record.onset if (record.source != "healthkit" and record.onset is not None
                                and math.isfinite(record.onset) and 0 <= record.onset <= 240) else None
    if beds:
      # An inBed wrapper that begins with the first asleep stage does not
      # establish latency, though its duration remains measured.
      latency = resolve_sleep_onset(record)
      if latency is None:
        latency = DEFAULT_ONSET
      bed_start, onset_source = first_stage - latency * 60, "estimated"
    elif pre_awake:
      bed_start, onset_source = min(pre_awake), "stage_awake"
      latency = (first_sleep - bed_start) / 60
    elif reported is not None:
      bed_start, onset_source = first_stage - reported * 60, "reported"
      latency = reported
    elif play_start is not None:
      bed_start, onset_source = play_start, "mindora_play"
      latency = (first_sleep - bed_start) / 60
    else:
      latency = resolve_sleep_onset(record)
      if latency is None or not math.isfinite(latency) or not 0 <= latency <= 240:
        latency = DEFAULT_ONSET
      bed_start, onset_source = first_stage - latency * 60, "estimated"
    tib = sum(b - a for a, b in beds) / 60 if beds else (last - bed_start) / 60
    source = "measured" if beds else "estimated"
  asleep = sum(b - a for a, b, kind in rows if kind != "awake") / 60
  if not 0 <= latency <= 240 or not asleep <= tib <= MAX_SPAN / 60:
    return None
  return {"first_sleep_ts": int(first_stage), "bed_start_ts": int(bed_start),
          "sleep_onset_minutes": int(latency), "time_in_bed_minutes": math.ceil(tib),
          "source": source, "sleep_onset_source": onset_source,
          "asleep_minutes": asleep}


def build_sleep_metrics(record: Optional[SleepResult], profile: Optional[UserProfile], tz) -> Optional[dict]:
  if record is None:
    return None
  estimate = estimate_sleep_window(record, profile)
  unspecified = has_unspecified_stage(record, profile)
  fallback_onset = resolve_sleep_onset(record) if not estimate else None
  result = {"sleep_onset_minutes": estimate["sleep_onset_minutes"] if estimate else fallback_onset,
            "time_in_bed_minutes": estimate["time_in_bed_minutes"] if estimate else None,
            "source": estimate["source"] if estimate else "estimated",
            "sleep_onset_source": estimate["sleep_onset_source"] if estimate else ("reported" if fallback_onset is not None else "unavailable"),
            "stage_classification": "contains_unspecified" if unspecified else "staged",
            "date": datetime.datetime.fromtimestamp(record.timestamp,tz).date().isoformat()}
  return result


def has_unspecified_stage(record: SleepResult, profile: Optional[UserProfile]) -> bool:
  """Expose source uncertainty without changing legacy core aggregation."""
  if any(s.sleep_type == "unspecified" for s in record.sleep_status):
    return True
  if record.source != "healthkit" or not profile or not record.sleep_status:
    return False
  first = min(s.start_time for s in record.sleep_status)
  last = max(s.start_time + s.duration * 60 for s in record.sleep_status)
  for sample in (profile.behaviors or {}).get("sleep_stage_unspecified", []) or []:
    if isinstance(sample, (list, tuple)) and len(sample) >= 2:
      try:
        start, duration = float(sample[0]), float(sample[1])
        if duration > 0 and start < last and start + duration > first:
          return True
      except (TypeError, ValueError, OverflowError):
        continue
  return False
