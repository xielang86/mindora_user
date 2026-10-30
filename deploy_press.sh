mkdir -p /tmp/mindora-loadtest/data

RUN_DIR=/tmp/mindora-loadtest \
USER_SERVER_PORT=19001 \
JWT_SECRET_KEY=mindora-local-load-test \
ENABLE_LLM=false \
ENABLE_SLEEP_RECO=false \
python user_server.py



