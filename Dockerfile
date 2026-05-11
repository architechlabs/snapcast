ARG BUILD_FROM=debian:bookworm-slim
FROM ${BUILD_FROM}

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PULSE_RUNTIME_PATH=/run/pulse \
    PULSE_STATE_PATH=/data/pulse \
    PULSE_COOKIE=/data/pulse/cookie

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        alsa-utils \
        bluez \
        ca-certificates \
        curl \
        dbus \
        ffmpeg \
        iproute2 \
        jq \
        libasound2-plugins \
        netcat-openbsd \
        procps \
        pulseaudio \
        pulseaudio-module-bluetooth \
        pulseaudio-utils \
        python3 \
        python3-aiohttp \
        python3-paho-mqtt \
        python3-yaml \
        snapserver \
        tini \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY rootfs/ /
RUN chmod +x /usr/local/bin/audio-hub-run /usr/local/bin/audio-hub-device-event \
    && chmod +x /opt/audio_hub/*.py

EXPOSE 1704 1705 1780 8099 5555 5556/udp

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/usr/local/bin/audio-hub-run"]

