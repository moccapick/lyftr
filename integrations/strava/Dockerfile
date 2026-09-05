FROM python:3.12-alpine
WORKDIR /app
COPY server.py /app/server.py
COPY fixtures /app/fixtures
ENV DATA_DIR=/app/data PORT=8090
HEALTHCHECK --interval=30s --timeout=5s CMD wget -qO- http://127.0.0.1:8090/health || exit 1
CMD ["python", "/app/server.py", "serve"]
