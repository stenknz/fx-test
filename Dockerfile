# fx-bot: paper FX trading on IG demo (bot loop + dashboard). Stdlib only.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/data
WORKDIR /app
COPY config.json ./
COPY bot_ig.py dashboard.py ig_client.py strategy.py trading.py fxdata.py check_ig.py close_paper.py ./
COPY strategies/ ./strategies/
RUN python3 -m py_compile bot_ig.py dashboard.py ig_client.py check_ig.py close_paper.py \
    && rm -rf __pycache__
VOLUME ["/data"]
EXPOSE 8082
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s \
  CMD python3 -c "import urllib.request;urllib.request.urlopen('http://localhost:8082/api/status',timeout=5)" || exit 1
CMD ["sh", "-c", "python3 dashboard.py --bind 0.0.0.0 --port 8082 & exec python3 bot_ig.py"]
