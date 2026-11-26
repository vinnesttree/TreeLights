# TreeLights: one image with the web app and the Python server (architecture §6.1).

# The web app is static files: build it on the build machine's own platform, so a
# cross-build for arm64 doesn't emulate Node.
FROM --platform=$BUILDPLATFORM node:22.12.0-bookworm-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# The native (Rust) kernels for walk processing and reconstruction, built as a wheel for
# the target platform (server/native). Same base as the runtime, so the wheel matches its
# glibc and Python.
FROM python:3.12.8-slim-bookworm AS native
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
      | sh -s -- -y --profile minimal --default-toolchain 1.97.0 \
    && /root/.cargo/bin/rustc --version
ENV PATH=/root/.cargo/bin:$PATH
RUN pip install --no-cache-dir maturin==1.16.0
COPY server/native /native
RUN cd /native && maturin build --release --locked -o /wheels

FROM python:3.12.8-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TREELIGHTS_DATA=/data \
    TREELIGHTS_WEB=/app/web
WORKDIR /app
COPY server/requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY --from=native /wheels /wheels
RUN pip install --no-cache-dir --no-deps /wheels/*.whl && rm -rf /wheels \
    && python -c "import treelights_native"
COPY server/treelights ./treelights
COPY --from=web /web/dist ./web
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh \
    && useradd --system --uid 1000 --home-dir /app treelights \
    && mkdir -p /data
# Default ports; TREELIGHTS_HTTPS_PORT / TREELIGHTS_HTTP_PORT change them (with host
# networking these are the host's ports). The health check follows the setting.
EXPOSE 8443 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import os,ssl,urllib.request;urllib.request.urlopen('https://127.0.0.1:%s/api/health'%(os.environ.get('TREELIGHTS_HTTPS_PORT') or '8443'),context=ssl._create_unverified_context(),timeout=4)"
ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "treelights"]
