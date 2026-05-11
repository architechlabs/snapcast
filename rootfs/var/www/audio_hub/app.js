const state = {
  config: null,
  status: null,
};

const $ = (id) => document.getElementById(id);
const ingressBase = () => {
  const path = window.location.pathname.replace(/\/$/, "");
  return path.startsWith("/app/") ? path : "";
};

async function api(path, options = {}) {
  const response = await fetch(`${ingressBase()}${path}`, {
    headers: { "content-type": "application/json" },
    ...options,
  });
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.json();
}

function tile(label, value) {
  return `<div class="tile"><strong>${label}</strong><span>${value ?? "unknown"}</span></div>`;
}

function render() {
  if (!state.status) return;
  const config = state.status.config || {};
  $("subtitle").textContent = state.status.summary || "Running";
  $("routingMode").value = config.audio?.routing_mode || "mix";
  $("latency").value = config.audio?.latency_ms || 120;
  $("latencyValue").textContent = `${$("latency").value} ms`;
  $("wiredVolume").value = Math.round((config.wired?.volume ?? 0.9) * 100);
  $("networkVolume").value = Math.round((config.network?.volume ?? 0.85) * 100);
  $("bluetoothVolume").value = Math.round((config.wireless?.volume ?? 0.85) * 100);

  const health = state.status.health || {};
  $("status").innerHTML = [
    tile("Pipeline", health.pipeline),
    tile("Snapcast", health.snapcast),
    tile("PulseAudio", health.pulse),
    tile("Active source", health.active_source),
    tile("Wired", health.wired_input),
    tile("Network", health.network_input),
    tile("Bluetooth", health.bluetooth_input),
    tile("Entities", health.entities),
  ].join("");

  $("devices").textContent = JSON.stringify(state.status.devices || {}, null, 2);
}

async function refresh() {
  try {
    state.status = await api("/api/status");
    render();
  } catch (err) {
    $("subtitle").textContent = `UI disconnected: ${err.message}`;
  }
}

async function patchConfig(payload) {
  await api("/api/config", {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
  await refresh();
}

$("restart").addEventListener("click", async () => {
  await api("/api/restart", { method: "POST", body: "{}" });
  await refresh();
});

$("routingMode").addEventListener("change", (event) => patchConfig({ audio: { routing_mode: event.target.value } }));
$("latency").addEventListener("input", (event) => $("latencyValue").textContent = `${event.target.value} ms`);
$("latency").addEventListener("change", (event) => patchConfig({ audio: { latency_ms: Number(event.target.value) } }));
$("wiredVolume").addEventListener("change", (event) => patchConfig({ wired: { volume: Number(event.target.value) / 100 } }));
$("networkVolume").addEventListener("change", (event) => patchConfig({ network: { volume: Number(event.target.value) / 100 } }));
$("bluetoothVolume").addEventListener("change", (event) => patchConfig({ wireless: { volume: Number(event.target.value) / 100 } }));
$("wiredToggle").addEventListener("click", () => patchConfig({ wired: { enabled: !state.status.config.wired.enabled } }));
$("networkToggle").addEventListener("click", () => patchConfig({ network: { enabled: !state.status.config.network.enabled } }));
$("bluetoothToggle").addEventListener("click", () => patchConfig({ wireless: { bluetooth_enabled: !state.status.config.wireless.bluetooth_enabled } }));

refresh();
setInterval(refresh, 5000);
