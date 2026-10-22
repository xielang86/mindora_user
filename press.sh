JWT_SECRET_KEY=mindora-local-load-test \
python tool/mixed_load_test.py \
  --base-url http://127.0.0.1:19001 \
  --duration 60 \
  --qps 900 \
  --concurrency 64  \
  --mix revision=1,profile=90,analysis=5,update=5
