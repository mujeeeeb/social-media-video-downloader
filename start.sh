#!/bin/bash
node /app/pot-server/build/main.js &
uvicorn main:app --host 0.0.0.0 --port $PORT
