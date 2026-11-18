ARG BUILD_FROM=docker.io/denoland/deno:2.9.7
FROM ${BUILD_FROM}

# Upstream: https://github.com/vrtmrz/livesync-bridge (no releases yet, so a fixed commit is pinned).
ARG BRIDGE_REF=1c252305bbddcacd200b8560d3bb786afe0906e9

WORKDIR /app
ADD https://github.com/vrtmrz/livesync-bridge/archive/${BRIDGE_REF}.tar.gz /tmp/bridge.tar.gz
RUN tar -xzf /tmp/bridge.tar.gz --strip-components=1 -C /app \
    && rm /tmp/bridge.tar.gz \
    && deno install --frozen

COPY gen_config.ts /gen_config.ts
COPY notes_server.ts /app/notes_server.ts
COPY notes_ui /app/notes_ui
COPY run.sh /run.sh
RUN chmod +x /run.sh

# The add-on runs as root so it can write to /share and /data.
USER root
ENV LSB_CONFIG=/data/config.json
ENTRYPOINT []
CMD [ "/run.sh" ]
