FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 bot && mkdir /data && chown bot:bot /data
COPY --chown=bot:bot bot.py dashboard_state.py /app/
USER bot
VOLUME ["/data"]
CMD ["python", "bot.py", "paper", "--db", "/data/paper.sqlite", "--report", "/data/paper-report.html"]
