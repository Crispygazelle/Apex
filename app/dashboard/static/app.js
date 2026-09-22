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

const SPEED_MAX_KMH = 160;
const G_MAX = 4;
const MAX_TRACK_POINTS = 15000;
const FAST_KMH = 70;
const BRAKE_MPS2 = -2.8;
const SPARK_MAX = 180;
// CARTO rather than openstreetmap.org: OSM's own servers are volunteer-funded
// and their usage policy does not cover an app hammering them, which they
// enforce by serving an "access blocked" image that caches like a real tile.
const REMOTE_TILE_URL = 'https://basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png';
const TILE_ATTRIBUTION = '© Esri · © OpenStreetMap contributors';
// Replaced by the locally cached layer when /api/map reports one.
let tileConfig = {
  tiles_cached: false, tile_url: '', tile_source: 'remote',
  min_zoom: 13, max_zoom: 16,
};
// A first tile off a cold cache can take a while on a phone hotspot, and
// losing the street map mid-ride is worse than a few seconds of grey.
const TILE_GRACE_MS = 12000;
const TILE_RETRY_MS = 15000;
const TILE_PROBE_URL = 'https://basemaps.cartocdn.com/dark_all/2/1/1.png';
const TRAIL = {
  fast: '#9b5de5',
  normal: '#3ecf8e',
  brake: '#e5484d',
};

const el = (id) => document.getElementById(id);

const ui = {
  helmet: el('helmet'), route: el('route'), destination: el('destination'),
  clock: el('clock'), state: el('state'), link: el('link'),
  tileSource: el('tileSource'), voiceArmed: el('voiceArmed'),
  directorToggle: el('directorToggle'),
  speed: el('speed'), maxSpeed: el('maxSpeed'), speedKind: el('speedKind'),
  speedSpark: el('speedSpark'), gSpark: el('gSpark'),
  fusion: el('fusion'), lean: el('lean'), leanBike: el('leanBike'),
  gforce: el('gforce'), maxG: el('maxG'),
  distance: el('distance'), remaining: el('remaining'), eta: el('eta'),
  gradient: el('gradient'), altitude: el('altitude'),
  heading: el('heading'), cardinal: el('cardinal'),
  longAccel: el('longAccel'), sats: el('sats'),
  posConf: el('posConf'), coords: el('coords'), diag: el('diag'),
  mapNote: el('mapNote'), map: el('map'), canvas: el('trackCanvas'),
  nowBanner: el('nowBanner'), nowLine: el('nowLine'),
  alert: el('alert'), alertTitle: el('alertTitle'), alertDetail: el('alertDetail'),
  alertCancel: el('alertCancel'), alertCount: el('alertCount'),
  rideLog: el('rideLog'),
  director: el('directorPanel'), directorPhrases: el('directorPhrases'),
  directorNote: el('directorNote'),
  recap: el('recap'), recapTitle: el('recapTitle'), recapStats: el('recapStats'),
  recapDismiss: el('recapDismiss'),
};

let track = [];
let hazards = [];
let lastSample = null;
let leafletMap = null;
let colorLines = [];
let brakeDots = [];
let hazardDots = [];
let marker = null;
let startMarker = null;
let destMarker = null;
let followRider = true;
let tileProbeTimer = null;
let framesReceived = 0;
let countdownTimer = null;
let logCount = 0;
const seenLogKeys = new Set();
const speedHist = [];
const gHist = [];
let rideOrigin = null;
let recapShown = false;
let routeLengthM = 0;
let nowTimer = null;
const directorBeats = [];
let beatIndex = 0;
const recapCounts = { brake: 0, hazard: 0, voice: 0, sos: false };

/* ---------- gauges ---------- */

function trailKind(s) {
  if (s.longitudinal_accel_mps2 <= BRAKE_MPS2 && s.speed_kmh > 8) return 'brake';
  if (s.speed_kmh >= FAST_KMH) return 'fast';
  return 'normal';
}

function cardinal(deg) {
  const dirs = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'];
  return dirs[Math.round(((deg % 360) + 360) % 360 / 45) % 8];
}

function pushSpark(speed, g) {
  speedHist.push(speed);
  gHist.push(g);
  if (speedHist.length > SPARK_MAX) speedHist.shift();
  if (gHist.length > SPARK_MAX) gHist.shift();
  drawSpark(ui.speedSpark, speedHist, SPEED_MAX_KMH, TRAIL.normal);
  drawSpark(ui.gSpark, gHist, G_MAX, TRAIL.fast);
}

function drawSpark(canvas, values, max, color) {
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const ratio = window.devicePixelRatio || 1;
  const w = canvas.clientWidth || canvas.width;
  const h = canvas.clientHeight || canvas.height;
  if (w < 8 || h < 8) return;
  canvas.width = w * ratio;
  canvas.height = h * ratio;
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, w, h);
  if (values.length < 2) return;
  ctx.beginPath();
  values.forEach((value, i) => {
    const x = (i / (values.length - 1)) * w;
    const y = h - 2 - (Math.max(0, Math.min(value, max)) / max) * (h - 4);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.6;
  ctx.lineJoin = 'round';
  ctx.stroke();
}

/* ---------- map ---------- */

function initMap() {
  if (window.__leafletFailed || typeof L === 'undefined') {
    // Leaflet itself never arrived, so there are no tiles to wait for.
    useCanvasFallback('Leaflet unavailable; drawing the track locally.', false);
    return;
  }

  ui.map.hidden = false;
  ui.canvas.hidden = true;

  const cached = tileConfig.tiles_cached;
  leafletMap = L.map('map', {
    zoomControl: false,
    attributionControl: true,
    // Zooming out past the cache would only reveal blank tiles.
    minZoom: cached ? tileConfig.min_zoom : 2,
  });
  leafletMap.setView([21.146, 79.085], cached ? 15 : 2);

  // Local tiles first. Remote CARTO is a last resort when this checkout
  // has no cache; a live demo must not discover it needs the internet.
  const tiles = cached
    ? L.tileLayer(tileConfig.tile_url, {
      maxZoom: 19,
      maxNativeZoom: tileConfig.max_zoom,
      minZoom: tileConfig.min_zoom,
      attribution: `${tileConfig.attribution || TILE_ATTRIBUTION} · cached offline`,
    })
    : L.tileLayer(REMOTE_TILE_URL, {
      maxZoom: 19,
      attribution: tileConfig.attribution || TILE_ATTRIBUTION,
    });

  let tileLoaded = false;
  tiles.on('tileload', () => { tileLoaded = true; });
  tiles.addTo(leafletMap);
  if (!cached) {
    setTimeout(() => {
      if (!tileLoaded && leafletMap) {
        useCanvasFallback('Offline: tiles unreachable, showing local track.', true);
      }
    }, TILE_GRACE_MS);
  }

  marker = L.circleMarker([0, 0], {
    radius: 7, color: '#fff', fillColor: '#f0883e', fillOpacity: 1, weight: 2,
  }).addTo(leafletMap);

  leafletMap.on('dragstart', () => { followRider = false; });
  requestAnimationFrame(() => {
    if (leafletMap) leafletMap.invalidateSize();
  });
}

/* Falling back is not a verdict: coverage comes and goes on a bike, so unless
 * Leaflet itself is missing we keep probing and restore the street map when
 * tiles answer again. */
function useCanvasFallback(note, recoverable) {
  if (leafletMap) { leafletMap.remove(); leafletMap = null; }
  colorLines = [];
  brakeDots = [];
  hazardDots = [];
  marker = startMarker = destMarker = null;

  ui.map.hidden = true;
  ui.canvas.hidden = false;
  ui.mapNote.textContent = note;
  drawCanvasTrack();

  if (recoverable && tileProbeTimer === null) {
    tileProbeTimer = setTimeout(probeTiles, TILE_RETRY_MS);
  }
}

function probeTiles() {
  tileProbeTimer = null;
  const probe = new Image();
  probe.onload = restoreLeafletMap;
  probe.onerror = () => { tileProbeTimer = setTimeout(probeTiles, TILE_RETRY_MS); };
  // Prefer the local cache probe. Hitting CARTO while tiles sit on disk
  // would make a venue outage look like a missing map.
  probe.src = tileConfig.probe_url || TILE_PROBE_URL;
}

async function loadMapConfig() {
  try {
    const response = await fetch('/api/map');
    if (response.ok) tileConfig = await response.json();
  } catch { /* fall through to the public tile server */ }
  setTileSource(tileConfig.tile_source || (tileConfig.tiles_cached ? 'cached' : 'remote'));
  return tileConfig;
}

function setTileSource(source) {
  ui.tileSource.textContent = source === 'cached' ? 'tiles local' : `tiles ${source}`;
  ui.tileSource.className = `pill ${source}`;
}

function restoreLeafletMap() {
  initMap();
  if (leafletMap === null) return;
  ui.mapNote.textContent = '';
  rebuildTrail();
}

/* The trail lives in `track`, not in the Leaflet layers, so it can be redrawn
 * from scratch after a fallback without losing any of the ride. */
function rebuildTrail() {
  let segment = null;
  let segmentKind = null;

  track.forEach((point, i) => {
    const latlng = [point.lat, point.lon];
    if (point.kind === segmentKind && segment !== null) {
      segment.addLatLng(latlng);
      return;
    }
    const seed = i > 0 ? [[track[i - 1].lat, track[i - 1].lon], latlng] : [latlng];
    segment = L.polyline(seed, trailStyle(point.kind)).addTo(leafletMap);
    colorLines.push(segment);
    segmentKind = point.kind;
    if (point.kind === 'brake' && i > 0) addBrakeDot(point.lat, point.lon);
  });

  hazards.forEach((hazard) => addHazardDot(hazard.lat, hazard.lon, hazard.label));

  const head = track[track.length - 1];
  if (head) {
    marker.setLatLng([head.lat, head.lon]);
    startMarker = L.circleMarker([track[0].lat, track[0].lon], {
      radius: 5, color: '#fff', fillColor: '#8b949e', fillOpacity: 1, weight: 2,
    }).addTo(leafletMap).bindTooltip('Start', { direction: 'top' });
    leafletMap.setView([head.lat, head.lon], 15);
  }
  if (lastSample) ensureDestMarker(lastSample);
}

function trailStyle(kind) {
  return {
    color: TRAIL[kind] || TRAIL.normal,
    weight: kind === 'brake' ? 5 : 4,
    opacity: 0.95,
    lineJoin: 'round',
    lineCap: 'round',
  };
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
  const lastLine = colorLines[colorLines.length - 1];
  const lastPoint = track[track.length - 1];
  if (lastLine && lastPoint && lastPoint.kind === kind) {
    lastLine.addLatLng([lat, lon]);
    return;
  }
  const latlngs = lastPoint ? [[lastPoint.lat, lastPoint.lon], [lat, lon]] : [[lat, lon]];
  colorLines.push(L.polyline(latlngs, trailStyle(kind)).addTo(leafletMap));
}

function pushTrackSample(s) {
  lastSample = s;
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
    recapCounts.voice += 1;
    body.innerHTML =
      `<div><span class="log-who">Rider</span> <span class="log-rider">${escapeHtml(message.rider || '')}</span></div>`
      + `<div><span class="log-who">Apex</span> <span class="log-apex">${escapeHtml(message.apex || '')}</span></div>`;
    if (message.intent === 'hazard' && message.latitude) {
      recapCounts.hazard += 1;
      const label = message.hazard_type || 'hazard';
      hazards.push({ lat: message.latitude, lon: message.longitude, label });
      addHazardDot(message.latitude, message.longitude, label);
    }
    flashNow(`Rider · ${message.rider || 'command'}`, 'voice');
  } else {
    body.innerHTML = `<div class="log-event-text">${escapeHtml(message.text || '')}</div>`;
    flashNow(message.text || message.kind || 'event', message.kind || '');
    if (message.kind === 'hard_brake' || message.kind === 'sudden_stop') recapCounts.brake += 1;
  }

  row.append(time, body);
  ui.rideLog.appendChild(row);
  ui.rideLog.scrollLeft = ui.rideLog.scrollWidth;
  logCount += 1;
  ui.nowLine.textContent = isVoice
    ? (message.apex || message.rider || 'voice')
    : (message.text || message.kind || 'event');

  if (message.kind === 'hard_brake' || message.kind === 'sudden_stop') {
    addBrakeDot(message.latitude, message.longitude);
  }
}

function flashNow(text, kind) {
  ui.nowBanner.hidden = false;
  ui.nowBanner.className = `now-banner ${kind || ''}`;
  ui.nowBanner.textContent = text;
  clearTimeout(nowTimer);
  nowTimer = setTimeout(() => { ui.nowBanner.hidden = true; }, 4200);
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

function formatEta(s) {
  if (!s.remaining_m || s.remaining_m < 80 || s.speed_kmh < 4) return '—';
  const seconds = s.remaining_m / (s.speed_kmh / 3.6);
  if (seconds < 60) return `${seconds.toFixed(0)} s`;
  return `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, '0')}`;
}

function formatClock(timestamp) {
  if (rideOrigin === null) rideOrigin = timestamp;
  const elapsed = Math.max(0, timestamp - rideOrigin);
  const minutes = Math.floor(elapsed / 60);
  const seconds = Math.floor(elapsed % 60);
  return `T+${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;
}

function render(s) {
  framesReceived += 1;
  if (rideOrigin === null) rideOrigin = s.timestamp;
  if (s.remaining_m > 80 && s.distance_m > 0) {
    routeLengthM = Math.max(routeLengthM, s.distance_m + s.remaining_m);
  }

  const kind = trailKind(s);
  ui.speed.textContent = s.speed_kmh.toFixed(0);
  ui.maxSpeed.textContent = s.max_speed_kmh.toFixed(0);
  ui.speed.parentElement.className = `speed-readout ${kind}`;
  ui.speedKind.textContent = kind === 'fast' ? 'fast' : kind === 'brake' ? 'braking' : 'cruise';

  ui.gforce.textContent = s.g_force.toFixed(2);
  ui.maxG.textContent = s.max_g_force.toFixed(2);

  ui.lean.textContent = s.lean_angle_deg.toFixed(1);
  ui.leanBike.setAttribute('transform', `rotate(${-s.lean_angle_deg.toFixed(1)})`);

  ui.distance.textContent = (s.distance_m / 1000).toFixed(2);
  ui.remaining.textContent = formatRemaining(s);
  ui.eta.textContent = formatEta(s);
  ui.gradient.textContent = s.gradient_pct.toFixed(1);
  ui.altitude.textContent = s.altitude_m.toFixed(0);
  ui.heading.textContent = s.heading_deg.toFixed(0);
  ui.cardinal.textContent = cardinal(s.heading_deg);
  ui.longAccel.textContent = s.longitudinal_accel_mps2.toFixed(2);
  ui.sats.textContent = s.satellites;
  ui.posConf.textContent = (s.position_confidence * 100).toFixed(0);
  ui.coords.textContent = s.gps_valid
    ? `${s.latitude.toFixed(5)}, ${s.longitude.toFixed(5)}`
    : 'no fix';

  const fusion = s.fusion_mode.replace(/_/g, ' ');
  ui.fusion.textContent = fusion;
  ui.clock.textContent = formatClock(s.timestamp);
  ui.state.textContent = s.system_state.replace(/_/g, ' ');
  ui.state.className = `pill ${s.system_state}`;

  pushSpark(s.speed_kmh, s.g_force);
  pushTrackSample(s);
  noteSample(s);
  updateDynReadouts(s);
  requestCharts();
  maybeRecap(s);

  ui.diag.textContent =
    `${framesReceived} frames · ${track.length} pts · ${fusion}`
    + ` · tiles ${tileConfig.tile_source || (tileConfig.tiles_cached ? 'local' : 'remote')}`;
}

function maybeRecap(s) {
  if (recapShown || !s.destination_latitude) return;
  if (s.distance_m < 200 || s.remaining_m >= 80) return;
  showRecap(s);
}

function showRecap(s) {
  recapShown = true;
  ui.recap.hidden = false;
  const dest = ui.destination.textContent && ui.destination.textContent !== '—'
    ? ui.destination.textContent
    : 'destination';
  ui.recapTitle.textContent = `Arrived · ${dest}`;
  const elapsed = rideOrigin === null ? 0 : Math.max(0, s.timestamp - rideOrigin);
  const travelled = routeLengthM > 0
    ? Math.min(s.distance_m, routeLengthM)
    : s.distance_m;
  const rows = [
    ['Distance', `${(travelled / 1000).toFixed(2)} km`],
    ['Duration', formatClock(s.timestamp).replace('T+', '')],
    ['Max speed', `${s.max_speed_kmh.toFixed(0)} km/h`],
    ['Peak g', `${s.max_g_force.toFixed(2)} g`],
    ['Brakes', String(recapCounts.brake)],
    ['Hazards', String(recapCounts.hazard)],
    ['Voice', String(recapCounts.voice)],
    ['SOS', recapCounts.sos ? 'rehearsed' : 'idle'],
    ['Elapsed', `${elapsed.toFixed(0)} s`],
  ];
  ui.recapStats.innerHTML = rows
    .map(([label, value]) => `<div><dt>${label}</dt><dd>${value}</dd></div>`)
    .join('');
}

function hideRecap() {
  ui.recap.hidden = true;
}

/* ---------- crash and SOS ---------- */

function showAlert(sos, event) {
  const state = sos.state;

  if (state === 'idle' || state === 'cancelled') {
    hideAlert(state === 'cancelled' ? 'SOS cancelled.' : '');
    return;
  }

  recapCounts.sos = true;
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
    ui.alertCount.hidden = false;
    let remaining = sos.remaining_s;
    const tick = () => {
      const whole = Math.max(0, Math.ceil(remaining));
      ui.alertCount.textContent = String(whole);
      ui.alertTitle.textContent = 'Crash detected — sending SOS';
      remaining -= 1;
      if (remaining < -1) clearInterval(countdownTimer);
    };
    tick();
    countdownTimer = setInterval(tick, 1000);
  } else {
    ui.alertCount.hidden = true;
    const titles = {
      dispatching: 'Sending SOS…',
      sent: 'SOS sent — help has been notified',
      failed: 'SOS failed to send — call for help manually',
    };
    ui.alertTitle.textContent = titles[state] || `SOS ${state}`;
  }
}

function hideAlert(note) {
  clearInterval(countdownTimer);
  ui.alert.hidden = true;
  ui.alertCount.hidden = true;
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
      pushSpark(s.speed_kmh, s.g_force);
      noteSample(s);
    });
    if (data.samples.length) {
      rideOrigin = data.samples[0].timestamp;
      render(data.samples[data.samples.length - 1]);
    }
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
    const sim = data.sensor_backend === 'sim' || data.sensor_backend === 'replay';
    ui.helmet.textContent = sim
      ? `${data.helmet_id} · ${data.sensor_backend}`
      : data.helmet_id;
    if (data.route) ui.route.textContent = data.route;
    if (data.destination) ui.destination.textContent = data.destination;
    if (data.tile_source) setTileSource(data.tile_source === 'none' ? 'none' : data.tile_source);
    if (data.voice) {
      ui.voiceArmed.hidden = false;
      ui.voiceArmed.textContent = 'voice';
      ui.voiceArmed.className = 'pill voice-on';
    }
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

/* ---------- demo director ---------- */

async function callDemo(path, note) {
  ui.directorNote.textContent = `${note}…`;
  try {
    const response = await fetch(path, { method: 'POST' });
    const body = await response.json().catch(() => ({}));
    ui.directorNote.textContent = response.ok
      ? `${note}: done`
      : `${note} refused: ${body.detail || response.status}`;
  } catch (err) {
    ui.directorNote.textContent = `${note} failed: ${err}`;
  }
}

function nextBeat() {
  if (beatIndex >= directorBeats.length) {
    ui.directorNote.textContent = 'No more scripted beats.';
    return;
  }
  directorBeats[beatIndex]();
  beatIndex += 1;
}

async function loadDirector() {
  let caps;
  try {
    const response = await fetch('/api/demo');
    if (!response.ok) return;
    caps = await response.json();
  } catch { return; }

  if (!caps.can_inject && !caps.can_speak) return;
  ui.directorToggle.hidden = false;

  ui.director.querySelectorAll('[data-demo]').forEach((button) => {
    const needsPath = button.dataset.demo === 'brake';
    button.disabled = !caps.can_inject || (needsPath && !caps.can_brake);
    button.addEventListener('click', () => {
      const kind = button.dataset.demo;
      callDemo(`/api/demo/${kind}`, button.textContent);
    });
  });
  if (caps.can_inject) {
    directorBeats.push(() => callDemo('/api/demo/pothole', 'Pothole'));
    if (caps.can_brake) {
      directorBeats.push(() => callDemo('/api/demo/brake', 'Hard brake'));
    }
  }
  if (!caps.can_inject) {
    ui.directorNote.textContent = 'Replaying a recording: events come from the record.';
  }

  (caps.can_speak ? caps.phrases || [] : []).forEach((phrase) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = phrase.replace(/^hey apex /i, '');
    const fire = () => callDemo(`/api/demo/say?text=${encodeURIComponent(phrase)}`, 'Said');
    button.addEventListener('click', fire);
    ui.directorPhrases.appendChild(button);
    directorBeats.push(fire);
  });
}

window.addEventListener('resize', () => {
  if (!ui.canvas.hidden) drawCanvasTrack();
  if (leafletMap) leafletMap.invalidateSize();
  if (lastSample) {
    drawSpark(ui.speedSpark, speedHist, SPEED_MAX_KMH, TRAIL.normal);
    drawSpark(ui.gSpark, gHist, G_MAX, TRAIL.fast);
  }
  if (dynamicsPage) drawDynamics();
});

document.addEventListener('keydown', (event) => {
  const tag = event.target.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'BUTTON') return;
  if (event.key === 'd' || event.key === 'D') {
    if (!ui.directorToggle.hidden) {
      ui.director.hidden = !ui.director.hidden;
    }
  }
  if (event.key === ' ' && directorBeats.length) {
    event.preventDefault();
    if (ui.director.hidden) ui.director.hidden = false;
    nextBeat();
  }
  if (event.key === 'Escape' && !ui.recap.hidden) hideRecap();
  if (event.key === '1') showPage('ride');
  if (event.key === '2') showPage('dynamics');
});

document.querySelectorAll('.nav-btn[data-page]').forEach((button) => {
  button.addEventListener('click', () => showPage(button.dataset.page));
});

ui.alertCancel.addEventListener('click', cancelSos);
ui.recapDismiss.addEventListener('click', hideRecap);
ui.directorToggle.addEventListener('click', () => {
  ui.director.hidden = !ui.director.hidden;
});

/* ---------- dynamics page ---------- */

const GRAVITY = 9.80665;
const SERIES_DT = 0.25;
const SERIES_CAP = 20000;
const series = [];
let dynamicsPage = false;
let chartDirty = false;

function showPage(page) {
  dynamicsPage = page === 'dynamics';
  document.body.dataset.page = page;
  const ride = document.getElementById('page-ride');
  const dynamics = document.getElementById('page-dynamics');
  if (ride) ride.hidden = dynamicsPage;
  if (dynamics) dynamics.hidden = !dynamicsPage;
  document.querySelectorAll('.nav-btn[data-page]').forEach((button) => {
    button.classList.toggle('active', button.dataset.page === page);
  });
  if (!dynamicsPage && leafletMap) {
    requestAnimationFrame(() => leafletMap.invalidateSize());
  }
  if (dynamicsPage) requestAnimationFrame(drawDynamics);
}

function noteSample(s) {
  if (!Number.isFinite(s.timestamp)) return;
  const last = series[series.length - 1];
  if (last && s.timestamp <= last.t) return;
  if (last && s.timestamp - last.t < SERIES_DT) return;
  const east = Number.isFinite(s.east_m) ? s.east_m : 0;
  const north = Number.isFinite(s.north_m) ? s.north_m : 0;
  series.push({
    t: s.timestamp,
    kmh: s.speed_kmh,
    v: s.speed_mps,
    ax: s.longitudinal_accel_mps2,
    ay: s.lateral_accel_mps2,
    lean: s.lean_angle_deg,
    east,
    north,
  });
  if (series.length > SERIES_CAP) series.shift();
}

function updateDynReadouts(s) {
  const speed = document.getElementById('dynSpeed');
  if (!speed) return;
  const ax = s.longitudinal_accel_mps2;
  const predicted = GRAVITY * Math.tan(s.lean_angle_deg * Math.PI / 180);
  speed.textContent = s.speed_kmh.toFixed(0);
  document.getElementById('dynAx').textContent = ax.toFixed(2);
  document.getElementById('dynEast').textContent = (s.east_m || 0).toFixed(0);
  document.getElementById('dynNorth').textContent = (s.north_m || 0).toFixed(0);
  document.getElementById('dynResidual').textContent = (s.lateral_accel_mps2 - predicted).toFixed(2);
  document.getElementById('dynPower').textContent = (ax * s.speed_mps).toFixed(1);
  const meta = document.getElementById('dynMeta');
  if (meta && series.length) {
    const span = series[series.length - 1].t - series[0].t;
    meta.textContent = `${series.length} samples · ${span.toFixed(0)} s`;
  }
}

function requestCharts() {
  if (!dynamicsPage || chartDirty) return;
  chartDirty = true;
  requestAnimationFrame(() => {
    chartDirty = false;
    if (dynamicsPage) drawDynamics();
  });
}

function drawDynamics() {
  drawLongitudinal(document.getElementById('chartSpeed'));
  drawLocalTrack(document.getElementById('chartTrack'));
  drawCoordinatedTurn(document.getElementById('chartTurn'));
  drawSpecificPower(document.getElementById('chartPower'));
}

function chartFrame(canvas) {
  if (!canvas) return null;
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  if (w < 8 || h < 8) return null;
  canvas.width = Math.floor(w * dpr);
  canvas.height = Math.floor(h * dpr);
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  ctx.font = '11px "IBM Plex Sans", system-ui, sans-serif';
  ctx.lineJoin = 'round';
  ctx.lineCap = 'round';
  return { ctx, w, h };
}

function drawEmpty(ctx, w, h, text) {
  ctx.fillStyle = '#8b95a2';
  ctx.textAlign = 'center';
  ctx.fillText(text, w / 2, h / 2);
}

function niceStep(span, ticks) {
  const raw = Math.abs(span) / Math.max(ticks, 1);
  if (!Number.isFinite(raw) || raw <= 0) return 1;
  const pow = 10 ** Math.floor(Math.log10(raw));
  const err = raw / pow;
  const nice = err >= 7.5 ? 10 : err >= 3.5 ? 5 : err >= 1.5 ? 2 : 1;
  return nice * pow;
}

function ticksBetween(min, max, count) {
  const step = niceStep(max - min, count);
  const start = Math.ceil(min / step) * step;
  const out = [];
  for (let value = start; value <= max + step * 0.01; value += step) out.push(value);
  return out;
}

function formatTick(value) {
  const abs = Math.abs(value);
  if (abs >= 100) return value.toFixed(0);
  if (abs >= 10) return value.toFixed(abs >= 20 ? 0 : 1);
  return value.toFixed(1);
}

function clockLabel(seconds) {
  const whole = Math.max(0, Math.round(seconds));
  const m = Math.floor(whole / 60);
  const s = whole % 60;
  return `${m}:${String(s).padStart(2, '0')}`;
}

function drawLongitudinal(canvas) {
  const frame = chartFrame(canvas);
  if (!frame) return;
  const { ctx, w, h } = frame;
  const pad = { l: 46, r: 46, t: 14, b: 26 };
  const iw = w - pad.l - pad.r;
  const ih = h - pad.t - pad.b;
  if (series.length < 2) {
    drawEmpty(ctx, w, h, 'Waiting for motion');
    return;
  }
  const t0 = series[0].t;
  const span = Math.max(series[series.length - 1].t - t0, 1);
  let vMax = 40;
  let aMin = -8;
  let aMax = 4;
  series.forEach((point) => {
    vMax = Math.max(vMax, point.kmh);
    aMin = Math.min(aMin, point.ax);
    aMax = Math.max(aMax, point.ax);
  });
  vMax *= 1.08;
  const xOf = (t) => pad.l + ((t - t0) / span) * iw;
  const ySpeed = (kmh) => pad.t + ih - (kmh / vMax) * ih;
  const yAccel = (a) => pad.t + ih - ((a - aMin) / (aMax - aMin)) * ih;

  ctx.strokeStyle = 'rgba(255,255,255,0.06)';
  ctx.fillStyle = '#8b95a2';
  ctx.textAlign = 'right';
  ctx.lineWidth = 1;
  ticksBetween(0, vMax, 4).forEach((tick) => {
    const y = ySpeed(tick);
    ctx.beginPath();
    ctx.moveTo(pad.l, y);
    ctx.lineTo(w - pad.r, y);
    ctx.stroke();
    ctx.fillText(formatTick(tick), pad.l - 6, y + 3);
  });
  ctx.textAlign = 'left';
  ticksBetween(aMin, aMax, 4).forEach((tick) => {
    ctx.fillText(formatTick(tick), w - pad.r + 6, yAccel(tick) + 3);
  });

  ctx.beginPath();
  series.forEach((point, i) => {
    const x = xOf(point.t);
    const y = ySpeed(point.kmh);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.lineTo(xOf(series[series.length - 1].t), pad.t + ih);
  ctx.lineTo(xOf(series[0].t), pad.t + ih);
  ctx.closePath();
  ctx.fillStyle = 'rgba(62, 207, 142, 0.16)';
  ctx.fill();
  ctx.beginPath();
  series.forEach((point, i) => {
    const x = xOf(point.t);
    const y = ySpeed(point.kmh);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.strokeStyle = '#3ecf8e';
  ctx.lineWidth = 1.6;
  ctx.stroke();

  ctx.beginPath();
  series.forEach((point, i) => {
    const x = xOf(point.t);
    const y = yAccel(point.ax);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.strokeStyle = '#f0883e';
  ctx.lineWidth = 1.5;
  ctx.stroke();

  const brakeY = yAccel(BRAKE_MPS2);
  ctx.setLineDash([4, 4]);
  ctx.strokeStyle = 'rgba(229, 72, 77, 0.85)';
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(pad.l, brakeY);
  ctx.lineTo(w - pad.r, brakeY);
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = '#e5484d';
  ctx.textAlign = 'left';
  ctx.fillText('brake', pad.l + 6, brakeY - 4);

  ctx.fillStyle = '#8b95a2';
  ctx.textAlign = 'center';
  ticksBetween(0, span, 4).forEach((tick) => {
    ctx.fillText(clockLabel(tick), xOf(t0 + tick), h - 8);
  });
}

function drawLocalTrack(canvas) {
  const frame = chartFrame(canvas);
  if (!frame) return;
  const { ctx, w, h } = frame;
  const pad = 36;
  if (series.length < 2) {
    drawEmpty(ctx, w, h, 'Waiting for a fix');
    return;
  }
  let minE = Infinity;
  let maxE = -Infinity;
  let minN = Infinity;
  let maxN = -Infinity;
  series.forEach((point) => {
    minE = Math.min(minE, point.east);
    maxE = Math.max(maxE, point.east);
    minN = Math.min(minN, point.north);
    maxN = Math.max(maxN, point.north);
  });
  const span = Math.max(maxE - minE, maxN - minN, 20);
  const midE = (minE + maxE) / 2;
  const midN = (minN + maxN) / 2;
  const scale = Math.min(w - pad * 2, h - pad * 2) / span;
  const project = (point) => [
    w / 2 + (point.east - midE) * scale,
    h / 2 - (point.north - midN) * scale,
  ];

  ctx.strokeStyle = 'rgba(255,255,255,0.06)';
  ctx.beginPath();
  ctx.moveTo(w / 2, pad / 2);
  ctx.lineTo(w / 2, h - pad / 2);
  ctx.moveTo(pad / 2, h / 2);
  ctx.lineTo(w - pad / 2, h / 2);
  ctx.stroke();
  ctx.fillStyle = '#8b95a2';
  ctx.textAlign = 'left';
  ctx.fillText(`${Math.round(span)} m`, 10, 16);

  ctx.lineWidth = 2.4;
  for (let i = 1; i < series.length; i += 1) {
    const kind = series[i].ax <= BRAKE_MPS2 && series[i].kmh > 8
      ? 'brake'
      : series[i].kmh >= FAST_KMH ? 'fast' : 'normal';
    ctx.strokeStyle = TRAIL[kind];
    const [x0, y0] = project(series[i - 1]);
    const [x1, y1] = project(series[i]);
    ctx.beginPath();
    ctx.moveTo(x0, y0);
    ctx.lineTo(x1, y1);
    ctx.stroke();
  }
  const [hx, hy] = project(series[series.length - 1]);
  ctx.fillStyle = '#f0883e';
  ctx.beginPath();
  ctx.arc(hx, hy, 4.5, 0, Math.PI * 2);
  ctx.fill();
}

function drawCoordinatedTurn(canvas) {
  const frame = chartFrame(canvas);
  if (!frame) return;
  const { ctx, w, h } = frame;
  const pad = { l: 46, r: 16, t: 14, b: 28 };
  const moving = series.filter((point) => point.v > 2 && Math.abs(point.lean) < 55);
  if (moving.length < 4) {
    drawEmpty(ctx, w, h, 'Waiting for a turn');
    return;
  }
  const points = moving.map((point) => ({
    pred: GRAVITY * Math.tan(point.lean * Math.PI / 180),
    meas: point.ay,
  }));
  let limit = 2;
  points.forEach((point) => {
    limit = Math.max(limit, Math.abs(point.pred), Math.abs(point.meas));
  });
  limit *= 1.15;
  const xOf = (value) => pad.l + ((value + limit) / (2 * limit)) * (w - pad.l - pad.r);
  const yOf = (value) => pad.t + (1 - (value + limit) / (2 * limit)) * (h - pad.t - pad.b);

  ctx.strokeStyle = 'rgba(255,255,255,0.06)';
  ctx.fillStyle = '#8b95a2';
  ctx.lineWidth = 1;
  ticksBetween(-limit, limit, 4).forEach((tick) => {
    ctx.beginPath();
    ctx.moveTo(xOf(tick), pad.t);
    ctx.lineTo(xOf(tick), h - pad.b);
    ctx.moveTo(pad.l, yOf(tick));
    ctx.lineTo(w - pad.r, yOf(tick));
    ctx.stroke();
    ctx.textAlign = 'center';
    ctx.fillText(formatTick(tick), xOf(tick), h - 8);
    ctx.textAlign = 'right';
    ctx.fillText(formatTick(tick), pad.l - 6, yOf(tick) + 3);
  });

  ctx.setLineDash([4, 4]);
  ctx.strokeStyle = 'rgba(255,255,255,0.55)';
  ctx.beginPath();
  ctx.moveTo(xOf(-limit), yOf(-limit));
  ctx.lineTo(xOf(limit), yOf(limit));
  ctx.stroke();
  ctx.setLineDash([]);

  points.forEach((point, index) => {
    const age = index / points.length;
    ctx.fillStyle = `rgba(126, 182, 255, ${0.25 + age * 0.75})`;
    ctx.beginPath();
    ctx.arc(xOf(point.pred), yOf(point.meas), 2.4, 0, Math.PI * 2);
    ctx.fill();
  });
}

function drawSpecificPower(canvas) {
  const frame = chartFrame(canvas);
  if (!frame) return;
  const { ctx, w, h } = frame;
  const pad = { l: 48, r: 16, t: 14, b: 26 };
  const iw = w - pad.l - pad.r;
  const ih = h - pad.t - pad.b;
  if (series.length < 2) {
    drawEmpty(ctx, w, h, 'Waiting for motion');
    return;
  }
  const powers = series.map((point) => point.ax * point.v);
  const t0 = series[0].t;
  const span = Math.max(series[series.length - 1].t - t0, 1);
  let peak = 8;
  powers.forEach((value) => { peak = Math.max(peak, Math.abs(value)); });
  peak *= 1.1;
  const xOf = (t) => pad.l + ((t - t0) / span) * iw;
  const yOf = (value) => pad.t + ih / 2 - (value / peak) * (ih / 2);

  ctx.strokeStyle = 'rgba(255,255,255,0.06)';
  ctx.fillStyle = '#8b95a2';
  ctx.lineWidth = 1;
  ctx.textAlign = 'right';
  ticksBetween(-peak, peak, 4).forEach((tick) => {
    const y = yOf(tick);
    ctx.beginPath();
    ctx.moveTo(pad.l, y);
    ctx.lineTo(w - pad.r, y);
    ctx.stroke();
    ctx.fillText(formatTick(tick), pad.l - 6, y + 3);
  });

  const zero = yOf(0);
  ctx.beginPath();
  powers.forEach((value, i) => {
    const x = xOf(series[i].t);
    const y = yOf(Math.max(0, value));
    if (i === 0) ctx.moveTo(x, zero);
    ctx.lineTo(x, y);
  });
  ctx.lineTo(xOf(series[series.length - 1].t), zero);
  ctx.closePath();
  ctx.fillStyle = 'rgba(62, 207, 142, 0.28)';
  ctx.fill();

  ctx.beginPath();
  powers.forEach((value, i) => {
    const x = xOf(series[i].t);
    const y = yOf(Math.min(0, value));
    if (i === 0) ctx.moveTo(x, zero);
    ctx.lineTo(x, y);
  });
  ctx.lineTo(xOf(series[series.length - 1].t), zero);
  ctx.closePath();
  ctx.fillStyle = 'rgba(229, 72, 77, 0.28)';
  ctx.fill();

  ctx.beginPath();
  powers.forEach((value, i) => {
    const x = xOf(series[i].t);
    const y = yOf(value);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.strokeStyle = '#e6edf3';
  ctx.lineWidth = 1.4;
  ctx.stroke();

  ctx.fillStyle = '#8b95a2';
  ctx.textAlign = 'center';
  ticksBetween(0, span, 4).forEach((tick) => {
    ctx.fillText(clockLabel(tick), xOf(t0 + tick), h - 8);
  });
}

// Tile source has to be known before the map is built, so this one is awaited.
loadMapConfig().then(() => {
  initMap();
  loadIdentity();
  loadSos();
  loadEvents();
  loadDirector();
  return loadHistory();
}).then(connect);
