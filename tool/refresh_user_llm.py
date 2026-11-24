"""Ask the running user server to regenerate one user's stored LLM results.

Usage: OPS_JWT_TOKEN=... python tool/refresh_user_llm.py --uid USER_UID
The token must belong to an ops admin or super admin.
"""

import argparse
import json
import os
import sys

import requests


def main() -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--uid", required=True, help="Exact target user UID")
  parser.add_argument("--url", default=os.getenv("USER_SERVER_URL", "http://127.0.0.1:9001"),
                      help="Running user server base URL")
  parser.add_argument("--timeout", type=int, default=600, help="Request timeout in seconds")
  args = parser.parse_args()
  token = os.getenv("OPS_JWT_TOKEN")
  if not token:
    parser.error("OPS_JWT_TOKEN is required (ops admin or super admin JWT)")
  if not args.uid.strip() or args.uid != args.uid.strip():
    parser.error("--uid must be an exact nonempty UID")

  session = requests.Session()
  session.trust_env = False
  try:
    response = session.post(
      args.url.rstrip("/") + "/ops/refresh_user_llm",
      json={"jwt_token": token, "uid": args.uid}, timeout=args.timeout,
    )
    result = response.json()
  except (requests.RequestException, ValueError) as exc:
    print(f"refresh request failed: {exc}", file=sys.stderr)
    return 1
  print(json.dumps(result, ensure_ascii=False, indent=2))
  return 0 if response.ok and result.get("code") == 0 else 1


if __name__ == "__main__":
  raise SystemExit(main())
