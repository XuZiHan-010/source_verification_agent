#!/bin/bash
set -e

source-verification-api &
API_PID=$!

source-verification-worker &
WORKER_PID=$!

wait -n $API_PID $WORKER_PID
kill $API_PID $WORKER_PID 2>/dev/null || true
