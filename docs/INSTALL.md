# Snapcast Audio Hub

This directory is a complete Home Assistant OS add-on. It accepts ALSA capture devices, mixes enabled sources inside PulseAudio, feeds an embedded Snapcast server, exposes a local web UI, and publishes Home Assistant entities with MQTT Discovery plus REST sensor fallback.

## Install

1. Copy this `addon` directory into a Home Assistant add-on repository or into the local HAOS `addons` share.
2. In Home Assistant, open Settings > Add-ons > Add-on Store > Check for updates.
3. Install `Snapcast Audio Hub`.
4. Plug in a USB audio interface, USB microphone, or line-in device.
5. Start the add-on and open its web UI.

The add-on uses `/dev/snd`, host networking, and USB access. These are required for reliable audio capture and Snapcast client discovery on HAOS.

## Audio Pipeline

ALSA capture device -> PulseAudio source -> PulseAudio null sink mixer -> sink monitor PCM FIFO -> embedded Snapserver -> Snapcast clients.

The actual audio mixing happens in PulseAudio before Snapcast. Home Assistant and Music Assistant are control planes only.

## Network Music Input

The TCP bridge accepts raw PCM:

```sh
ffmpeg -re -i song.mp3 -f s16le -ar 48000 -ac 2 tcp://HOME_ASSISTANT_IP:5555
```

RTP can be enabled in options for senders that emit L16 RTP.

## Snapcast

Snapcast ports:

- `1704/tcp`: Snapcast client audio stream
- `1705/tcp`: Snapcast JSON-RPC
- `1780/tcp`: Snapcast HTTP UI/API

Add Snapcast clients on speakers, tablets, or other devices and point them at the HAOS host IP.

## Home Assistant Entities

With MQTT available, the add-on publishes MQTT Discovery entities. Without MQTT, it publishes REST state sensors to Home Assistant Core through the Supervisor API.

Controls that require commands need MQTT or the add-on web UI. REST fallback is status-only because Home Assistant cannot create persistent command entities from an add-on without MQTT or a custom integration.

## Music Assistant

Use Music Assistant as a source/controller, not as the mixer. Send its output to a Snapcast-compatible target/client or to a network bridge feeding this add-on. Grouping remains Snapcast-side for synchronized playback.

## Bluetooth

Bluetooth A2DP receiver support is best effort. HAOS often owns the Bluetooth adapter and host BlueZ D-Bus. If the adapter can be exposed to the add-on, enable `wireless.bluetooth_enabled` and `wireless.bluetooth_pairable`. If not, use the TCP/RTP network bridge as the self-contained wireless alternative.

## Recovery

The add-on restarts PulseAudio, bridge processes, and Snapserver when the health loop detects a degraded pipeline. USB device reconnects can be handled by restarting the pipeline from the web UI or MQTT button.

