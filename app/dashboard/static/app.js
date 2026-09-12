/* APEX dashboard client.
 *
 * No build step and no framework: this has to be served off a Pi Zero and
 * edited over SSH. Telemetry arrives over the WebSocket; history is fetched
 * once on load so the track is not empty.
 *
 * The map degrades on purpose. A helmet is regularly out of coverage, so if
 * Leaflet or its tiles cannot be reached the track is drawn on a canvas
 * instead, which needs no network at all.
 */

const GAUGE_ARC_LENGTH = 251.3; // length of the 180-degree r=80 arc path
const SPEED_MAX_KMH = 160;
const G_MAX = 4;
const MAX_TRACK_POINTS = 15000;
const FAST_KMH = 70;
const BRAKE_MPS2 = -2.8;
const TRAIL = {
  fast: '#9b5de5',
  normal: '#3ecf8e',
  brake: '#e5484d',
};

const el = (id) => document.getElementById(id);

const ui = {
  helmet: el('helmet'), ride: el('ride'), state: el('state'), link: el('link'),
  speed: el('speed'), maxSpeed: el('maxSpeed'), speedArc: el('speedArc'),
  fusion: el('fusion'), lean: el('lean'), leanBike: el('leanBike'),
  gforce: el('gforce'), maxG: el('maxG'), gArc: el('gArc'),
  distance: el('distance'), remaining: el('remaining'),
  gradient: el('gradient'), altitude: el('altitude'),
  heading: el('heading'), longAccel: el('longAccel'), sats: el('sats'),
  posConf: el('posConf'), coords: el('coords'), diag: el('diag'),
  mapNote: el('mapNote'), map: el('map'), canvas: el('trackCanvas'),
  alert: el('alert'), alertTitle: el('alertTitle'), alertDetail: el('alertDetail'),
  alertCancel: el('alertCancel'), rideLog: el('rideLog'),
};

let track = [];
let leafletMap = null;
let colorLines = [];
let brakeDots = [];
let hazardDots = [];
let marker = null;
let startMarker = null;
let destMarker = null;
let followRider = true;
let framesReceived = 0;
let countdownTimer = null;
let logCount = 0;
const seenLogKeys = new Set();

/* ---------- gauges ---------- */

function setArc(path, fraction) {
  const clamped = Math.max(0, Math.min(1, fraction));
  path.style.strokeDasharray = GAUGE_ARC_LENGTH;
  path.style.strokeDashoffset = GAUGE_ARC_LENGTH * (1 - clamped);
}

function gColour(g) {
  if (g >= 2.5) return 'var(--bad)';
  if (g >= 1.6) return 'var(--warn)';
  return 'var(--good)';
}

function trailKind(s) {
  if (s.longitudinal_accel_mps2 <= BRAKE_MPS2 && s.speed_kmh > 8) return 'brake';
  if (s.speed_kmh >= FAST_KMH) return 'fast';
  return 'normal';
}

/* ---------- map ---------- */

function initMap() {
  if (window.__leafletFailed || typeof L === 'undefined') {
    useCanvasFallback('Map tiles unavailable; drawing the track locally.');
    return;
  }

  leafletMap = L.map('map', { zoomControl: true, attributionControl: false });
  leafletMap.setView([20, 0], 2);

  const tiles = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
  });
  let tileLoaded = false;
  tiles.on('tileload', () => { tileLoaded = true; });
  tiles.addTo(leafletMap);
  setTimeout(() => {
    if (!tileLoaded) useCanvasFallback('Offline: tiles unreachable, showing local track.');
  }, 4000);

  marker = L.circleMarker([0, 0], {
    radius: 7, color: '#fff', fillColor: '#f0883e', fillOpacity: 1, weight: 2,
  }).addTo(leafletMap);

  leafletMap.on('dragstart', () => { followRider = false; });
}

function useCanvasFallback(note) {
  if (leafletMap) { leafletMap.remove(); leafletMap = null; }
  ui.map.hidden = true;
  ui.canvas.hidden = false;
  ui.mapNote.textContent = note;
  drawCanvasTrack();
}

function projectFactory(points) {
  const lats = points.map((p) => p.lat);
  const lons = points.map((p) => p.lon);
  const [minLat, maxLat] = [Math.min(...lats), Math.max(...lats)];
  const [minLon, maxLon] = [Math.min(...lons), Math.max(...lons)];
  const pad = 24;
  const w = ui.canvas.clientWidth - pad * 2;
  const h = ui.canvas.clientHeight - pad * 2;
  const lonScale = Math.cos((minLat + maxLat) / 2 * Math.PI / 180);
  const spanLat = Math.max(maxLat - minLat, 1e-6);
  const spanLon = Math.max((maxLon - minLon) * lonScale, 1e-6);
  const scale = Math.min(w / spanLon, h / spanLat);
  return (point) => [
    pad + w / 2 + (point.lon - (minLon + maxLon) / 2) * lonScale * scale,
    pad + h / 2 - (point.lat - (minLat + maxLat) / 2) * scale,
  ];
}

function drawCanvasTrack() {
  const canvas = ui.canvas;
  const ctx = canvas.getContext('2d');
  const ratio = window.devicePixelRatio || 1;

  canvas.width = canvas.clientWidth * ratio;
  canvas.height = canvas.clientHeight * ratio;
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, canvas.clientWidth, canvas.clientHeight);

  if (track.length < 2) return;

  const project = projectFactory(track);
  ctx.lineWidth = 3;
  ctx.lineJoin = 'round';
  ctx.lineCap = 'round';

  for (let i = 1; i < track.length; i += 1) {
    ctx.strokeStyle = TRAIL[track[i].kind] || TRAIL.normal;
    ctx.beginPath();
    const [x0, y0] = project(track[i - 1]);
    const [x1, y1] = project(track[i]);
    ctx.moveTo(x0, y0);
    ctx.lineTo(x1, y1);
    ctx.stroke();
    if (track[i].kind === 'brake' && track[i - 1].kind !== 'brake') {
      ctx.fillStyle = TRAIL.brake;
      ctx.beginPath();
      ctx.arc(x1, y1, 3.5, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  const [hx, hy] = project(track[track.length - 1]);
  ctx.fillStyle = '#fff';
  ctx.beginPath();
  ctx.arc(hx, hy, 4.5, 0, Math.PI * 2);
  ctx.fill();
}

function ensureDestMarker(s) {
  if (!leafletMap || destMarker || !s.destination_latitude) return;
  destMarker = L.circleMarker(
    [s.destination_latitude, s.destination_longitude],
    { radius: 8, color: '#fff', fillColor: '#58a6ff', fillOpacity: 0.95, weight: 2 },
  ).addTo(leafletMap).bindTooltip('Destination', { direction: 'top' });
}

function addBrakeDot(lat, lon) {
  if (!leafletMap) return;
  const last = brakeDots[brakeDots.length - 1];
  if (last) {
    const prev = last.getLatLng();
    const dlat = prev.lat - lat;
    const dlon = prev.lng - lon;
    if ((dlat * dlat + dlon * dlon) < 1e-10) return;
  }
  brakeDots.push(L.circleMarker([lat, lon], {
    radius: 5, color: '#ffb4b0', fillColor: TRAIL.brake, fillOpacity: 1, weight: 1.5,
  }).addTo(leafletMap));
}

function addHazardDot(lat, lon, label) {
  if (!leafletMap || !lat) return;
  hazardDots.push(L.circleMarker([lat, lon], {
    radius: 6, color: '#fff', fillColor: '#d29922', fillOpacity: 1, weight: 2,
  }).addTo(leafletMap).bindTooltip(label || 'hazard', { direction: 'top' }));
}

function appendColourSegment(lat, lon, kind) {
  const color = TRAIL[kind] || TRAIL.normal;
  const lastLine = colorLines[colorLines.length - 1];
  const lastPoint = track[track.length - 1];
  if (lastLine && lastPoint && lastPoint.kind === kind) {
    lastLine.addLatLng([lat, lon]);
    return;
  }
  const latlngs = lastPoint ? [[lastPoint.lat, lastPoint.lon], [lat, lon]] : [[lat, lon]];
  const line = L.polyline(latlngs, {
    color,
    weight: kind === 'brake' ? 5 : 4,
    opacity: 0.95,
    lineJoin: 'round',
    lineCap: 'round',
  }).addTo(leafletMap);
  colorLines.push(line);
}

function pushTrackSample(s) {
  if (!s.gps_valid || (!s.latitude && !s.longitude)) return;

  const kind = trailKind(s);
  const last = track[track.length - 1];
  if (last && Math.abs(last.lat - s.latitude) < 1e-6 && Math.abs(last.lon - s.longitude) < 1e-6) {
    return;
  }

  const firstPoint = track.length === 0;
  if (last && kind === 'brake' && last.kind !== 'brake') {
    addBrakeDot(s.latitude, s.longitude);
  }

  if (leafletMap) {
    appendColourSegment(s.latitude, s.longitude, kind);
    marker.setLatLng([s.latitude, s.longitude]);
    if (firstPoint) {
      startMarker = L.circleMarker([s.latitude, s.longitude], {
        radius: 5, color: '#fff', fillColor: '#8b949e', fillOpacity: 1, weight: 2,
      }).addTo(leafletMap).bindTooltip('Start', { direction: 'top' });
      leafletMap.setView([s.latitude, s.longitude], 15);
    } else if (followRider) {
      leafletMap.panTo([s.latitude, s.longitude], { animate: false });
    }
  }

  track.push({ lat: s.latitude, lon: s.longitude, kind });
  if (track.length > MAX_TRACK_POINTS) track.shift();

  ensureDestMarker(s);
  if (!leafletMap && !ui.canvas.hidden) drawCanvasTrack();
}

/* ---------- log box ---------- */

function logKey(message) {
  return `${message.type}|${message.timestamp}|${message.rider || ''}|${message.text || message.apex || ''}`;
}

function appendLog(message) {
  const key = logKey(message);
  if (seenLogKeys.has(key)) return;
  seenLogKeys.add(key);

  if (ui.rideLog.querySelector('.log-empty')) ui.rideLog.innerHTML = '';

  const row = document.createElement('div');
  const isVoice = message.type === 'voice';
  row.className = `log-row ${isVoice ? 'voice' : `event ${message.kind || ''}`}`;

  const clock = message.clock || new Date((message.timestamp || 0) * 1000).toLocaleTimeString();
  const time = document.createElement('div');
  time.className = 'log-time';
  time.textContent = clock;

  const body = document.createElement('div');
  body.className = 'log-body';
  if (isVoice) {
    body.innerHTML =
      `<div><span class="log-who">Rider</span> <span class="log-rider">${escapeHtml(message.rider || '')}</span></div>`
      + `<div><span class="log-who">Apex</span> <span class="log-apex">${escapeHtml(message.apex || '')}</span></div>`;
    if (message.intent === 'hazard' && message.latitude) {
      addHazardDot(message.latitude, message.longitude, message.hazard_type || 'hazard');
    }
  } else {
    body.innerHTML = `<div class="log-event-text">${escapeHtml(message.text || '')}</div>`;
  }

  row.append(time, body);
  ui.rideLog.appendChild(row);
  ui.rideLog.scrollTop = ui.rideLog.scrollHeight;
  logCount += 1;

  if (message.kind === 'hard_brake' || message.kind === 'sudden_stop') {
    addBrakeDot(message.latitude, message.longitude);
  }
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/* ---------- rendering ---------- */

function formatRemaining(s) {
  if (!s.destination_latitude && !s.remaining_m) return '—';
  if (s.remaining_m < 80) return 'arrived';
  if (s.remaining_m < 1000) return `${s.remaining_m.toFixed(0)} m`;
  return `${(s.remaining_m / 1000).toFixed(2)} km`;
}

function render(s) {
  framesReceived += 1;

  ui.speed.textContent = s.speed_kmh.toFixed(0);
  ui.maxSpeed.textContent = s.max_speed_kmh.toFixed(0);
  setArc(ui.speedArc, s.speed_kmh / SPEED_MAX_KMH);

  ui.gforce.textContent = s.g_force.toFixed(2);
  ui.maxG.textContent = s.max_g_force.toFixed(2);
  ui.gArc.style.stroke = gColour(s.g_force);
  setArc(ui.gArc, s.g_force / G_MAX);

  ui.lean.textContent = s.lean_angle_deg.toFixed(1);
  ui.leanBike.setAttribute('transform', `rotate(${-s.lean_angle_deg.toFixed(1)})`);

  ui.distance.textContent = (s.distance_m / 1000).toFixed(3);
  ui.remaining.textContent = formatRemaining(s);
  ui.gradient.textContent = s.gradient_pct.toFixed(1);
  ui.altitude.textContent = s.altitude_m.toFixed(0);
  ui.heading.textContent = s.heading_deg.toFixed(0);
  ui.longAccel.textContent = s.longitudinal_accel_mps2.toFixed(2);
  ui.sats.textContent = s.satellites;
  ui.posConf.textContent = (s.position_confidence * 100).toFixed(0);
  ui.coords.textContent = s.gps_valid
    ? `${s.latitude.toFixed(5)}, ${s.longitude.toFixed(5)}`
    : 'no fix';

  const fusion = s.fusion_mode.replace(/_/g, ' ');
  ui.ride.textContent = s.ride_id;
  ui.fusion.textContent = fusion;
  ui.state.textContent = s.system_state.replace(/_/g, ' ');
  ui.state.className = `pill ${s.system_state}`;

  pushTrackSample(s);

  ui.diag.textContent =
    `${framesReceived} frames · ${track.length} track points · fusion ${fusion}`;
}

/* ---------- crash and SOS ---------- */

function showAlert(sos, event) {
  const state = sos.state;

  if (state === 'idle' || state === 'cancelled') {
    hideAlert(state === 'cancelled' ? 'SOS cancelled.' : '');
    return;
  }

  ui.alert.hidden = false;
  ui.alert.className = `alert ${state}`;
  ui.alertCancel.hidden = state !== 'countdown';

  const detail = event
    ? `${event.peak_g.toFixed(1)} g · ${event.speed_before_kmh.toFixed(0)} → `
      + `${event.speed_after_kmh.toFixed(0)} km/h · ${event.reason}`
    : '';
  ui.alertDetail.textContent = detail;

  clearInterval(countdownTimer);
  if (state === 'countdown') {
    let remaining = sos.remaining_s;
    const tick = () => {
      ui.alertTitle.textContent =
        `CRASH DETECTED — sending SOS in ${Math.max(0, remaining).toFixed(0)}s`;
      remaining -= 1;
      if (remaining < -1) clearInterval(countdownTimer);
    };
    tick();
    countdownTimer = setInterval(tick, 1000);
  } else {
    const titles = {
      dispatching: 'SENDING SOS…',
      sent: 'SOS SENT — help has been notified',
      failed: 'SOS FAILED TO SEND — call for help manually',
    };
    ui.alertTitle.textContent = titles[state] || `SOS ${state}`;
  }
}

function hideAlert(note) {
  clearInterval(countdownTimer);
  ui.alert.hidden = true;
  if (note) ui.diag.textContent = note;
}

async function cancelSos() {
  ui.alertCancel.disabled = true;
  try {
    const response = await fetch('/api/sos/cancel', { method: 'POST' });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      ui.alertDetail.textContent = body.detail || 'Cancel was refused.';
    }
  } catch (err) {
    ui.alertDetail.textContent = `Cancel failed: ${err}`;
  } finally {
    ui.alertCancel.disabled = false;
  }
}

/* ---------- transport ---------- */

function setLink(up) {
  ui.link.textContent = up ? 'live' : 'offline';
  ui.link.className = `pill ${up ? 'link-up' : 'link-down'}`;
}

function handleMessage(message) {
  if (message.type === 'voice' || message.type === 'event') {
    appendLog(message);
    return;
  }
  if (message.sos) {
    showAlert(message.sos, message.event);
    return;
  }
  render(message);
}

function connect() {
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
  const socket = new WebSocket(`${scheme}://${location.host}/ws/telemetry`);

  socket.onopen = () => {
    setLink(true);
    socket.__ping = setInterval(() => {
      if (socket.readyState === WebSocket.OPEN) socket.send('ping');
    }, 5000);
  };

  socket.onmessage = (event) => {
    try {
      handleMessage(JSON.parse(event.data));
    } catch (err) {
      console.error('bad telemetry frame', err);
    }
  };

  const teardown = () => {
    clearInterval(socket.__ping);
    setLink(false);
    setTimeout(connect, 2000);
  };
  socket.onclose = teardown;
  socket.onerror = () => socket.close();
}

async function loadHistory() {
  try {
    const response = await fetch('/api/history?seconds=900&step=4');
    if (!response.ok) return;
    const data = await response.json();
    data.samples.forEach((s) => {
      if (s.gps_valid) pushTrackSample(s);
    });
    if (data.samples.length) render(data.samples[data.samples.length - 1]);
  } catch (err) {
    console.warn('no history available yet', err);
  }
}

async function loadEvents() {
  try {
    const response = await fetch('/api/events');
    if (!response.ok) return;
    const data = await response.json();
    (data.events || []).forEach(appendLog);
  } catch (err) {
    console.warn('no event log yet', err);
  }
}

async function loadIdentity() {
  try {
    const response = await fetch('/api/health');
    if (!response.ok) return;
    const data = await response.json();
    ui.ride.textContent = data.route || data.ride_id;
    ui.helmet.textContent =
      data.sensor_backend === 'sim' ? `${data.helmet_id} (sim)` : data.helmet_id;
  } catch { /* the dashboard still works without it */ }
}

async function loadSos() {
  try {
    const response = await fetch('/api/sos');
    if (!response.ok) return;
    const sos = await response.json();
    if (sos.enabled && sos.state !== 'idle') showAlert(sos, sos.event);
  } catch { /* the safety layer may be disabled */ }
}

window.addEventListener('resize', () => {
  if (!ui.canvas.hidden) drawCanvasTrack();
});

ui.alertCancel.addEventListener('click', cancelSos);

initMap();
loadIdentity();
loadSos();
loadEvents();
loadHistory().then(connect);
