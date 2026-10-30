JWT_SECRET_KEY=mindora-local-load-test \
python tool/mixed_load_test.py \
  --base-url http://127.0.0.1:19001 \
  --seed-profiles \
  --duration 60 \
  --qps 1000 \
  --concurrency 64 \
  --output /tmp/load-result.json
