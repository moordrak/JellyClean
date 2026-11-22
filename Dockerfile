FROM python:3.12-alpine
LABEL org.opencontainers.image.title="JellyClean" \
      org.opencontainers.image.description="Pregled i čišćenje prostora na Jellyfin poslužitelju" \
      org.opencontainers.image.source="https://github.com/moordrak/JellyClean"
RUN apk add --no-cache openssh-client
WORKDIR /app
COPY app.py /app/
COPY static /app/static
ENV PORT=8095 PYTHONUNBUFFERED=1 DATA_DIR=/data
EXPOSE 8095
HEALTHCHECK --interval=60s --timeout=5s CMD wget -qO- http://127.0.0.1:${PORT}/api/health >/dev/null || exit 1
CMD ["python", "/app/app.py"]
