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
const MAX_TRACK_POINTS = 3000;

const el = (id) => document.getElementById(id);

const ui = {
  helmet: el('helmet'), ride: el('ride'), state: el('state'), link: el('link'),
  speed: el('speed'), maxSpeed: el('maxSpeed'), speedArc: el('speedArc'),
  fusion: el('fusion'), lean: el('lean'), leanBike: el('leanBike'),
  gforce: el('gforce'), maxG: el('maxG'), gArc: el('gArc'),
  distance: el('distance'), gradient: el('gradient'), altitude: el('altitude'),
  heading: el('heading'), longAccel: el('longAccel'), sats: el('sats'),
  posConf: el('posConf'), coords: el('coords'), diag: el('diag'),
  mapNote: el('mapNote'), map: el('map'), canvas: el('trackCanvas'),
};

let track = [];
let leafletMap = null;
let trackLine = null;
let marker = null;
let followRider = true;
let framesReceived = 0;

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

/* ---------- map ---------- */

function initMap() {
  if (window.__leafletFailed || typeof L === 'undefined') {
    useCanvasFallback('Map tiles unavailable; drawing the track locally.');
    return;
  }

  leafletMap = L.map('map', { zoomControl: true, attributionControl: false });
  // A whole-world view until the first fix arrives. Seeding a specific city
  // here would hardcode wherever the simulator happens to start and show a
  // real rider the wrong place until their GPS locks.
  leafletMap.setView([20, 0], 2);

  const tiles = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
  });
  // If no tile loads we are offline: swap to the canvas rather than showing
  // an empty grey square.
  let tileLoaded = false;
  tiles.on('tileload', () => { tileLoaded = true; });
  tiles.addTo(leafletMap);
  setTimeout(() => {
    if (!tileLoaded) useCanvasFallback('Offline: tiles unreachable, showing local track.');
  }, 4000);

  trackLine = L.polyline([], { color: '#f0883e', weight: 3 }).addTo(leafletMap);
  marker = L.circleMarker([0, 0], {
    radius: 6, color: '#fff', fillColor: '#f0883e', fillOpacity: 1, weight: 2,
  }).addTo(leafletMap);

  // Panning implies the rider wants to look around, so stop recentring.
  leafletMap.on('dragstart', () => { followRider = false; });
}

function useCanvasFallback(note) {
  if (leafletMap) { leafletMap.remove(); leafletMap = null; }
  ui.map.hidden = true;
  ui.canvas.hidden = false;
  ui.mapNote.textContent = note;
  drawCanvasTrack();
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

  const lats = track.map((p) => p[0]);
  const lons = track.map((p) => p[1]);
  const [minLat, maxLat] = [Math.min(...lats), Math.max(...lats)];
  const [minLon, maxLon] = [Math.min(...lons), Math.max(...lons)];

  const pad = 24;
  const w = canvas.clientWidth - pad * 2;
  const h = canvas.clientHeight - pad * 2;
  // Keep the aspect ratio honest: latitude and longitude degrees are not the
  // same distance, and a stretched track misrepresents the route.
  const lonScale = Math.cos((minLat + maxLat) / 2 * Math.PI / 180);
  const spanLat = Math.max(maxLat - minLat, 1e-6);
  const spanLon = Math.max((maxLon - minLon) * lonScale, 1e-6);
  const scale = Math.min(w / spanLon, h / spanLat);

  const project = ([lat, lon]) => [
    pad + w / 2 + (lon - (minLon + maxLon) / 2) * lonScale * scale,
    pad + h / 2 - (lat - (minLat + maxLat) / 2) * scale,
  ];

  ctx.strokeStyle = '#f0883e';
  ctx.lineWidth = 2.5;
  ctx.lineJoin = 'round';
  ctx.beginPath();
  track.forEach((point, i) => {
    const [x, y] = project(point);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.stroke();

  const [hx, hy] = project(track[track.length - 1]);
  ctx.fillStyle = '#fff';
  ctx.beginPath();
  ctx.arc(hx, hy, 4.5, 0, Math.PI * 2);
  ctx.fill();
}

function pushTrackPoint(lat, lon) {
  if (!lat && !lon) return;

  const last = track[track.length - 1];
  // Skip points the rider has not meaningfully moved between; at 10 Hz this
  // is most of them, and the line does not need that density.
  if (last && Math.abs(last[0] - lat) < 1e-6 && Math.abs(last[1] - lon) < 1e-6) return;

  const firstPoint = track.length === 0;
  track.push([lat, lon]);
  if (track.length > MAX_TRACK_POINTS) track.shift();

  if (leafletMap) {
    trackLine.setLatLngs(track);
    marker.setLatLng([lat, lon]);
    if (firstPoint) {
      // Jump from the world view to the rider once we know where they are.
      leafletMap.setView([lat, lon], 16);
    } else if (followRider) {
      leafletMap.panTo([lat, lon], { animate: false });
    }
  } else if (!ui.canvas.hidden) {
    drawCanvasTrack();
  }
}

/* ---------- rendering ---------- */

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
  // Negative lean is a left turn; rotate the icon the same way the bike goes.
  ui.leanBike.setAttribute('transform', `rotate(${-s.lean_angle_deg.toFixed(1)})`);

  ui.distance.textContent = (s.distance_m / 1000).toFixed(3);
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

  if (s.gps_valid) pushTrackPoint(s.latitude, s.longitude);

  ui.diag.textContent =
    `${framesReceived} frames · ${track.length} track points · fusion ${fusion}`;
}

/* ---------- transport ---------- */

function setLink(up) {
  ui.link.textContent = up ? 'live' : 'offline';
  ui.link.className = `pill ${up ? 'link-up' : 'link-down'}`;
}

function connect() {
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
  const socket = new WebSocket(`${scheme}://${location.host}/ws/telemetry`);

  socket.onopen = () => {
    setLink(true);
    // The server reads from the socket to detect disconnects; a periodic ping
    // keeps that read fed and proves the link both ways.
    socket.__ping = setInterval(() => {
      if (socket.readyState === WebSocket.OPEN) socket.send('ping');
    }, 5000);
  };

  socket.onmessage = (event) => {
    try {
      render(JSON.parse(event.data));
    } catch (err) {
      console.error('bad telemetry frame', err);
    }
  };

  const teardown = () => {
    clearInterval(socket.__ping);
    setLink(false);
    setTimeout(connect, 2000); // the Pi may simply be rebooting
  };
  socket.onclose = teardown;
  socket.onerror = () => socket.close();
}

async function loadHistory() {
  try {
    const response = await fetch('/api/history?seconds=120&step=5');
    if (!response.ok) return;
    const data = await response.json();
    data.samples.filter((s) => s.gps_valid).forEach((s) => pushTrackPoint(s.latitude, s.longitude));
    if (data.samples.length) render(data.samples[data.samples.length - 1]);
  } catch (err) {
    console.warn('no history available yet', err);
  }
}

async function loadIdentity() {
  try {
    const response = await fetch('/api/health');
    if (!response.ok) return;
    const data = await response.json();
    ui.ride.textContent = data.ride_id;
    ui.helmet.textContent =
      data.sensor_backend === 'sim' ? `${data.helmet_id} (sim)` : data.helmet_id;
  } catch { /* the dashboard still works without it */ }
}

window.addEventListener('resize', () => {
  if (!ui.canvas.hidden) drawCanvasTrack();
});

initMap();
loadIdentity();
loadHistory().then(connect);
