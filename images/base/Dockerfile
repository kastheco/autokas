ARG NODE_VERSION
FROM node:${NODE_VERSION}-bookworm-slim
ARG BUN_VERSION
ARG OMP_VERSION
ARG TARGETARCH
RUN test "$TARGETARCH" = amd64 && test -n "$BUN_VERSION" && test -n "$OMP_VERSION" \
    && apt-get update \
    && apt-get install -y --no-install-recommends git gh curl unzip ca-certificates build-essential python3 \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt \
    && curl -fsSL "https://github.com/oven-sh/bun/releases/download/bun-v${BUN_VERSION}/bun-linux-x64.zip" -o /tmp/bun.zip \
    && unzip /tmp/bun.zip -d /tmp \
    && install /tmp/bun-linux-x64/bun /usr/local/bin/bun \
    && rm -rf /tmp/bun.zip /tmp/bun-linux-x64 \
    && BUN_INSTALL=/opt/bun bun install --global "@oh-my-pi/pi-coding-agent@${OMP_VERSION}" \
    && npm install --global corepack && corepack enable \
    && rm -rf /root/.npm /root/.cache /root/.bun \
    && rm -f /root/.bashrc /root/.profile /root/.wget-hsts /home/node/.bashrc /home/node/.profile /home/node/.bash_logout \
    && chmod 755 /root /etc/ssl/private /var/cache/ldconfig \
    && mkdir -p /workspace/repo /state /opt/autokas/skills \
    && chown -R node:node /workspace /state
ENV PATH="/opt/bun/bin:/usr/local/bin:/usr/bin:/bin" BUN_INSTALL=/opt/bun
COPY images/smoke.py /opt/autokas/smoke.py
COPY images/base/settings.json /opt/autokas/settings.json
USER node
WORKDIR /workspace/repo
CMD ["omp"]
