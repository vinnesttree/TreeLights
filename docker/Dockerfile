# TreeLights: one image with the web app and the Python server (architecture §6.1).

# The web app is static files: build it on the build machine's own platform, so a
# cross-build for arm64 doesn't emulate Node.
FROM --platform=$BUILDPLATFORM node:22.12.0-bookworm-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

FROM python:3.12.8-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TREELIGHTS_DATA=/data \
    TREELIGHTS_WEB=/app/web
WORKDIR /app
COPY server/requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY server/treelights ./treelights
COPY --from=web /web/dist ./web
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh \
    && useradd --system --uid 1000 --home-dir /app treelights \
    && mkdir -p /data
EXPOSE 8443 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import ssl,urllib.request;urllib.request.urlopen('https://127.0.0.1:8443/api/health',context=ssl._create_unverified_context(),timeout=4)"
ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "treelights"]
