# Troubleshooting

## No wired input

Open the add-on web UI and check Audio Devices. If no capture device appears, verify the USB audio interface is visible to HAOS and restart the add-on after plugging it in.

## Snapcast clients connect but hear silence

Check that PulseAudio, Snapcast, and Pipeline all report `running`. Confirm the source is enabled and the routing mode includes that source.

## TCP music input does not connect

The TCP bridge accepts one raw PCM sender at a time. Send `s16le`, `48000 Hz`, `2 channels` unless you changed add-on options.

## Bluetooth does not work

This is a HAOS platform constraint in many installs. Bluetooth may be owned by the host stack and unavailable to containers. Use TCP/RTP network input as the supported self-contained wireless path.

## Entity controls missing

Install and configure the Home Assistant MQTT integration and an MQTT broker add-on. Without MQTT, the add-on can still publish REST status sensors and provide controls in the add-on web UI.

