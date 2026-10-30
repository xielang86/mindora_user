#!/usr/bin/env python3
"""Mixed read/write HTTP load test for user_server.

The default mix is revision:70, profile:15, analysis:10, update:5.  Remote
targets are rejected unless --allow-remote is explicitly supplied.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import random
import statistics
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
import jwt


OPS = ("revision", "profile", "analysis", "update")


def parse_mix(value: str) -> dict[str, float]:
  result = {}
  try:
    for item in value.split(","):
      name, weight = item.split("=", 1)
      name, weight = name.strip(), float(weight)
      if name not in OPS or weight < 0:
        raise ValueError
      result[name] = weight
  except ValueError:
    raise argparse.ArgumentTypeError("mix must look like revision=70,profile=15,analysis=10,update=5")
  if not result or sum(result.values()) <= 0:
    raise argparse.ArgumentTypeError("mix must contain a positive weight")
  return result


def percentile(values: list[float], pct: float) -> float:
  if not values:
    return 0.0
  ordered = sorted(values)
  rank = max(0, math.ceil(pct / 100 * len(ordered)) - 1)
  return ordered[rank]


def local_target(url: str) -> bool:
  host = (urlparse(url).hostname or "").lower()
  return host in {"127.0.0.1", "localhost", "::1"}


class LoadTest:
  def __init__(self, args, profiles: list[dict]):
    self.args = args
    self.profiles = profiles
    self.rng = random.Random(args.seed)
    self.tokens = {
      row["uid"]: jwt.encode(
        {"uid": row["uid"], "email": f'{row["uid"]}@example.invalid',
         "iat": int(time.time()), "exp": int(time.time()) + 86400},
        args.jwt_secret, algorithm="HS256",
      ) for row in profiles
    }
    self.results = defaultdict(lambda: {"latency_ms": [], "ok": 0, "errors": 0, "bytes": 0})
    self.sequence = 0

  def auth(self, uid: str) -> dict:
    return {"jwt_token": self.tokens[uid]}

  def payload(self, op: str, row: dict) -> tuple[str, dict]:
    uid = row["uid"]
    now = int(time.time())
    base = {"timestamp": now, "version": "1.0"}
    if op == "revision":
      return "/user_profile", {**base, "request_type": "query_revision", "data": self.auth(uid)}
    if op == "profile":
      return "/user_profile", {**base, "request_type": "query_profile", "data": {
        **self.auth(uid), "sleep_data_count": 7, "include_behaviors": True,
        "analysis_report_count": 1, "include_mindora_record": True, "engagement_count": 0,
      }}
    if op == "analysis":
      return "/analysis", {**base, "request_type": "analysis_explore", "data": {
        **self.auth(uid), "timezone": "Asia/Shanghai", "language": "zh-Hans",
        "date": time.strftime("%Y-%m-%d"),
        "modules": ["score_summary", "onset_efficiency", "sleep_structure", "night_fluctuation"],
      }}
    self.sequence += 1
    return "/user_profile", {**base, "request_type": "update_profile", "data": {
      **self.auth(uid), "timezone": "Asia/Shanghai", "language": "zh-Hans",
      "skip_sleep_scenarios_reco_update": True, "skip_sleep_analysis_update": True,
      "user_profile": {"behaviors": {"clicks": [[now, {
        "event": "mixed_load_test", "sequence": self.sequence,
      }]]}},
    }}

  async def request(self, session: aiohttp.ClientSession, op: str, row: dict, record: bool = True) -> bool:
    path, payload = self.payload(op, row)
    started = time.perf_counter_ns()
    ok, size = False, 0
    try:
      async with session.post(self.args.base_url + path, json=payload) as response:
        body = await response.read()
        size = len(body)
        try:
          app_code = json.loads(body).get("code")
        except (json.JSONDecodeError, AttributeError):
          app_code = None
        ok = response.status == 200 and app_code == 0
    except (aiohttp.ClientError, asyncio.TimeoutError):
      ok = False
    elapsed = (time.perf_counter_ns() - started) / 1_000_000
    if record:
      bucket = self.results[op]
      bucket["latency_ms"].append(elapsed)
      bucket["ok" if ok else "errors"] += 1
      bucket["bytes"] += size
    return ok

  async def seed_profiles(self, session: aiohttp.ClientSession) -> None:
    semaphore = asyncio.Semaphore(min(self.args.concurrency, 16))

    async def seed_one(row):
      async with semaphore:
        uid = row["uid"]
        payload = {
          "request_type": "update_profile", "timestamp": int(time.time()), "version": "1.0",
          "data": {**self.auth(uid), "timezone": "Asia/Shanghai", "language": "zh-Hans",
                   "skip_sleep_scenarios_reco_update": True, "skip_sleep_analysis_update": True,
                   "user_profile": row["user_profile"]},
        }
        started = time.perf_counter_ns()
        try:
          async with session.post(self.args.base_url + "/user_profile", json=payload) as response:
            body = await response.read()
            parsed = json.loads(body)
            if response.status != 200 or parsed.get("code") != 0:
              return uid, f"HTTP {response.status}: {body[:200]!r}"
        except Exception as exc:
          return uid, str(exc)
        return uid, (time.perf_counter_ns() - started) / 1_000_000

    outcomes = await asyncio.gather(*(seed_one(row) for row in self.profiles))
    failures = [(uid, result) for uid, result in outcomes if isinstance(result, str)]
    if failures:
      raise RuntimeError(f"profile seed failed ({len(failures)}): {failures[:3]}")
    latency = [result for _uid, result in outcomes]
    print(f"seeded={len(outcomes)} p50={percentile(latency, 50):.2f}ms p95={percentile(latency, 95):.2f}ms")

  def choose_op(self) -> str:
    names, weights = zip(*self.args.mix.items())
    return self.rng.choices(names, weights=weights, k=1)[0]

  async def run(self, session: aiohttp.ClientSession) -> float:
    semaphore = asyncio.Semaphore(self.args.concurrency)
    pending: set[asyncio.Task] = set()

    async def one(op, row):
      async with semaphore:
        await self.request(session, op, row)

    # A short unrecorded warmup avoids measuring connection establishment.
    for _ in range(min(self.args.concurrency, len(self.profiles))):
      await self.request(session, "revision", self.rng.choice(self.profiles), record=False)

    started = time.perf_counter()
    deadline = started + self.args.duration
    next_at = started
    while time.perf_counter() < deadline:
      if self.args.qps > 0:
        next_at += 1 / self.args.qps
        delay = next_at - time.perf_counter()
        if delay > 0:
          await asyncio.sleep(delay)
      task = asyncio.create_task(one(self.choose_op(), self.rng.choice(self.profiles)))
      pending.add(task)
      task.add_done_callback(pending.discard)
      # Bound scheduler backlog when the server cannot sustain target QPS.
      if len(pending) >= self.args.concurrency * 4:
        await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
    if pending:
      await asyncio.gather(*pending)
    return time.perf_counter() - started

  def report(self, elapsed: float) -> dict:
    report = {"elapsed_seconds": round(elapsed, 3), "target_qps": self.args.qps,
              "concurrency": self.args.concurrency, "mix": self.args.mix, "operations": {}}
    all_latencies = []
    total_ok = total_errors = total_bytes = 0
    for op in OPS:
      bucket = self.results[op]
      values = bucket["latency_ms"]
      if not values:
        continue
      all_latencies.extend(values)
      total_ok += bucket["ok"]
      total_errors += bucket["errors"]
      total_bytes += bucket["bytes"]
      report["operations"][op] = self._stats(values, bucket["ok"], bucket["errors"], bucket["bytes"], elapsed)
    report["overall"] = self._stats(all_latencies, total_ok, total_errors, total_bytes, elapsed)
    return report

  @staticmethod
  def _stats(values, ok, errors, size, elapsed):
    return {
      "requests": len(values), "ok": ok, "errors": errors,
      "qps": round(len(values) / elapsed, 2), "bytes": size,
      "latency_ms": {
        "mean": round(statistics.fmean(values), 3),
        "p50": round(percentile(values, 50), 3), "p90": round(percentile(values, 90), 3),
        "p95": round(percentile(values, 95), 3), "p99": round(percentile(values, 99), 3),
        "max": round(max(values), 3),
      },
    }


async def async_main(args) -> dict:
  data = json.loads(Path(args.profiles).read_text(encoding="utf-8"))
  profiles = data.get("profiles") or []
  if not profiles:
    raise SystemExit("profile JSON contains no profiles")
  test = LoadTest(args, profiles)
  timeout = aiohttp.ClientTimeout(total=args.timeout)
  connector = aiohttp.TCPConnector(limit=args.concurrency, ttl_dns_cache=300)
  async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
    if args.seed_profiles:
      await test.seed_profiles(session)
    elapsed = await test.run(session)
  return test.report(elapsed)


def main() -> None:
  parser = argparse.ArgumentParser(description="Mixed user_server read/write load test")
  parser.add_argument("--base-url", default="http://127.0.0.1:9001")
  parser.add_argument("--profiles", default="tool/load_profiles_100.json")
  parser.add_argument("--jwt-secret", default=os.getenv("JWT_SECRET_KEY"),
                      help="Must match server JWT_SECRET_KEY; defaults to the environment variable")
  parser.add_argument("--mix", type=parse_mix, default=parse_mix("revision=70,profile=15,analysis=10,update=5"))
  parser.add_argument("--duration", type=float, default=30)
  parser.add_argument("--qps", type=float, default=100, help="Target aggregate arrival rate; 0 runs closed-loop")
  parser.add_argument("--concurrency", type=int, default=32)
  parser.add_argument("--timeout", type=float, default=10)
  parser.add_argument("--seed", type=int, default=316)
  parser.add_argument("--seed-profiles", action="store_true")
  parser.add_argument("--allow-remote", action="store_true")
  parser.add_argument("--output", help="Optional JSON result path")
  args = parser.parse_args()
  if not args.jwt_secret:
    parser.error("--jwt-secret or JWT_SECRET_KEY is required")
  if args.duration <= 0 or args.qps < 0 or args.concurrency <= 0:
    parser.error("duration/concurrency must be positive and qps must be non-negative")
  if not local_target(args.base_url) and not args.allow_remote:
    parser.error("remote target refused; pass --allow-remote only when explicitly authorized")
  args.base_url = args.base_url.rstrip("/")
  report = asyncio.run(async_main(args))
  print(json.dumps(report, ensure_ascii=False, indent=2))
  if args.output:
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
  main()
