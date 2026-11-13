# Profiler image with every optional tool included (scc, jscpd, gitleaks, tree-sitter).
#   docker compose run --rm profiler            settings from .env, zip in ./output
#   docker run --rm -v "$PWD/output:/out" --env-file .env gilddata/code-profiler:2.0.1
# Versions are pinned on purpose so numbers are reproducible. Builds on linux/amd64 and linux/arm64.
FROM python:3.11-slim

ARG SCC_VERSION=4.1.0
ARG GITLEAKS_VERSION=8.30.1
ARG JSCPD_VERSION=4.0.5

# Works on amd64 and arm64 hosts (Apple Silicon): the release asset is picked from the build architecture.
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates curl nodejs npm \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g jscpd@${JSCPD_VERSION} \
 && ARCH="$(dpkg --print-architecture)" \
 && case "$ARCH" in amd64) SCC_ARCH=x86_64; GL_ARCH=x64 ;; arm64) SCC_ARCH=arm64; GL_ARCH=arm64 ;; \
      *) echo "unsupported architecture: $ARCH" && exit 1 ;; esac \
 && curl -fsSL "https://github.com/boyter/scc/releases/download/v${SCC_VERSION}/scc_Linux_${SCC_ARCH}.tar.gz" \
    | tar -xz -C /usr/local/bin scc \
 && curl -fsSL "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_${GL_ARCH}.tar.gz" \
    | tar -xz -C /usr/local/bin gitleaks \
 && git config --system --add safe.directory '*' \
 && scc --version && gitleaks version && jscpd --version

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN pip install --no-cache-dir .

RUN chmod +x /app/docker-entrypoint.sh
ENTRYPOINT ["/app/docker-entrypoint.sh"]
