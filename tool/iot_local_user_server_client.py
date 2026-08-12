import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
  sys.path.insert(0, str(ROOT_DIR))

import iot_server
from tool.user_server_client import DEFAULT_DEBUG_UID, UserServerClient


def build_iot_local_base_url(
  local_name: str | None = None,
  port: int | None = None,
  host: str | None = None,
) -> str:
  server_port = port or int(os.getenv("IOT_USER_SERVER_PORT", iot_server.USER_SERVER_PORT))
  if host:
    return f"http://{host.rstrip('/')}:{server_port}"
  name = (local_name or os.getenv("IOT_DEVICE_LOCAL_NAME") or iot_server.MDNS_LOCAL_NAME).strip()
  return f"http://{name.rstrip('/')}:{server_port}"


def print_result(title: str, result: Any):
  print(f"\n{'=' * 20} {title} {'=' * 20}")
  print(json.dumps(result, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser(
    description="Example client that calls user_server through iot_server's mDNS local name."
  )
  parser.add_argument(
    "action",
    nargs="?",
    default="query_profile",
    choices=["query_profile", "update_profile", "run_all"],
  )
  parser.add_argument("--host", default=None,
                      help="user_server IP/hostname，如 192.168.1.207；传入后跳过 mDNS local name")
  parser.add_argument("--local-name", default=None, help="mDNS host, default: MyDevice.local")
  parser.add_argument("--port", type=int, default=None, help="user_server port, default: 9001")
  parser.add_argument("--uid", default=DEFAULT_DEBUG_UID)
  parser.add_argument("--jwt-token", default=os.getenv("JWT_TOKEN", ""))
  parser.add_argument("--timeout", type=int, default=10)
  parser.add_argument("--skip-sleep-scenarios-reco-update", action="store_true")
  return parser


def main():
  args = build_parser().parse_args()
  base_url = build_iot_local_base_url(args.local_name, args.port, host=args.host)
  client = UserServerClient(
    base_url=base_url,
    jwt_token=args.jwt_token,
    uid=args.uid,
    timeout=args.timeout,
  )

  if args.action == "run_all":
    result = client.run_all()
  elif args.action == "update_profile":
    result = client.update_profile(
      skip_sleep_scenarios_reco_update=args.skip_sleep_scenarios_reco_update
    )
  else:
    result = client.query_profile()

  print_result(f"{args.action} via {base_url}", result)


if __name__ == "__main__":
  main()
