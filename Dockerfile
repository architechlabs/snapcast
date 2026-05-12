ARG BUILD_FROM=ghcr.io/home-assistant/base:latest
FROM ${BUILD_FROM}

ENV PYTHONUNBUFFERED=1 \
    PULSE_STATE_PATH=/data/pulse

SHELL ["/bin/ash", "-eo", "pipefail", "-c"]

RUN sed -i -E 's/^#(.*\/community)$/\1/' /etc/apk/repositories \
    && apk add --no-cache \
        alsa-utils \
        alsa-plugins-pulse \
        bash \
        bluez \
        ca-certificates \
        coreutils \
        curl \
        dbus \
        ffmpeg \
        iproute2 \
        jq \
        lsof \
        netcat-openbsd \
        psmisc \
        procps \
        pulseaudio \
        pulseaudio-alsa \
        pulseaudio-bluez \
        pulseaudio-utils \
        python3 \
        py3-aiohttp \
        py3-paho-mqtt \
        py3-yaml \
        snapcast-client \
        snapcast-server

COPY rootfs/ /
RUN addgroup pulse audio 2>/dev/null || true \
    && chmod +x /usr/local/bin/audio-hub-run /usr/local/bin/audio-hub-device-event \
    && chmod +x /opt/audio_hub/*.py

EXPOSE 41704 41705 41780 41888 5555 5556/udp

CMD ["/usr/local/bin/audio-hub-run"]
