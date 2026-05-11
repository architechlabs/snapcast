# Entity Map

MQTT Discovery creates these entities using the base topic `snapcast_audio_hub`.

| Entity | Type | Purpose |
| --- | --- | --- |
| `sensor.snapcast_audio_hub_pipeline` | Sensor | Overall pipeline health |
| `sensor.snapcast_audio_hub_active_source` | Sensor | Current routing mode/source composition |
| `sensor.snapcast_audio_hub_wired_input` | Sensor | ALSA capture availability |
| `sensor.snapcast_audio_hub_snapcast` | Sensor | Embedded Snapserver health |
| `switch.snapcast_audio_hub_wired_enabled` | Switch | Enable or disable wired input |
| `switch.snapcast_audio_hub_network_enabled` | Switch | Enable or disable network bridge input |
| `switch.snapcast_audio_hub_bluetooth_enabled` | Switch | Enable or disable Bluetooth input |
| `select.snapcast_audio_hub_routing_mode` | Select | `mix`, `wired_only`, `network_only`, `bluetooth_only`, `fallback_duck`, `source_switch` |
| `number.snapcast_audio_hub_latency_ms` | Number | PulseAudio loopback latency |
| `number.snapcast_audio_hub_wired_volume` | Number | Wired source gain |
| `number.snapcast_audio_hub_network_volume` | Number | Network source gain |
| `button.snapcast_audio_hub_restart_pipeline` | Button | Restart audio processes |
| `button.snapcast_audio_hub_remove_entities` | Button | Remove retained MQTT Discovery entities |

REST fallback creates status-only sensors:

- `sensor.snapcast_audio_hub_pipeline`
- `sensor.snapcast_audio_hub_active_source`
- `sensor.snapcast_audio_hub_wired_input`
- `sensor.snapcast_audio_hub_snapcast`
