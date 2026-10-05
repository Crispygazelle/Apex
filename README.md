# APEX

Edge telemetry and a hands-free voice assistant for a motorcycle helmet, running on a Raspberry Pi Zero 2 W.

One process. An asyncio loop owns fusion, the dashboard, storage, safety and voice. Each sensor sits on its own blocking thread and hands frozen readings across. Develop on a Mac against simulated sensors; deploy the same tree to the Pi for hardware.

Python **3.11**. Raspberry Pi OS Bookworm ships 3.11. Vosk and Piper wheels are unreliable on 3.14.

---

## What it actually does

- **Fusion.** MPU6050 at 100 Hz and NEO-M8N at 10 Hz go through a 4-state constant-velocity Kalman filter in a local ENU frame. IMU acceleration is the control input. GPS is a full-matrix update with Joseph-form covariance and 4σ innovation gating. During a GPS blackout the filter dead-reckons.
- **Metrics.** Speed, g-force, lean (gyro roll corrected by `tan(lean) = v·ω/g` — an accelerometer cannot see lean in a coordinated turn), gradient, odometer from *filtered* speed (summing raw fixes is the GPS odometer bug).
- **Dashboard.** FastAPI on port 8000. Three pages: **Ride** (live map and gauges), **Dynamics** (speed, braking, lean, specific power), **Rides** (saved rides on this computer). Keys `1`, `2`, and `3` switch pages. Leaflet uses the offline tile cache, with a canvas track if tiles are missing.
- **Storage.** Every ride is written to `data/rides/` and can be reopened from the Rides page. InfluxDB v2 is optional. When it is unreachable, a size-bounded JSONL spool on disk is replayed on reconnect. Crash and hazard records are written to disk unconditionally.
- **Crash / SOS.** A 4 g spike is only the trigger. Confirmation needs a speed drop (absolute or proportional) and a still helmet. Then a 30 s cancellable countdown and a webhook payload. A spoken "emergency" cannot fire SOS on its own.
- **Voice.** Wake word → Vosk → a small phrase list → Piper. Raw PCM is dropped after transcription. Crash state seizes the session: cancel works without the wake word.

---

## Hardware

| Part | Role | Bus |
|---|---|---|
| Raspberry Pi Zero 2 W | Quad-core Cortex-A53 @ 1 GHz, **512 MB RAM** | — |
| MPU6050 | 6-axis IMU | I2C `0x68` |
| NEO-M8N | GNSS | UART `/dev/ttyAMA0` 9600 |
| INMP441 | I2S MEMS mic | I2S |
| 18650 + TP4056 | 5 V to the Pi | USB / GPIO |

The Zero 2 W is not "ARMv7 dual-core". The extra cores are why the threaded design fits; 512 MB is the ceiling once Vosk is loaded.

### Wiring

```
MPU6050     VCC→3V3  GND→GND  SDA→GPIO2  SCL→GPIO3
NEO-M8N     VCC→5V   GND→GND  TX→GPIO15  RX→GPIO14
INMP441     VCC→3V3  GND→GND  SCK→GPIO18  WS→GPIO19  SD→GPIO20
```

---

## Laptop and Pi use the same program

`config/apex.yaml` sets `sensor_backend: sim`, and turns the dashboard and voice on. A laptop therefore invents a Nagpur ride. The Pi does not edit that file. Its service adds `--backend hardware`, which reads the MPU6050, NEO-M8N, and microphone instead. Fusion, the dashboard, and the saved-ride files are the same either way.

| | Laptop | Raspberry Pi |
|---|---|---|
| Sensors | Simulated Sitabuldi → Airport ride | Real IMU, GPS, and mic |
| Command | `python -m app.main --no-voice` | `python -m app.main --backend hardware` |
| Dashboard | http://127.0.0.1:8000 | port 8000 on the Pi's address |

`--dashboard` is optional. The YAML already serves the dashboard. `--no-voice` is for a machine that does not have the speech models yet. Voice is on in the YAML, and startup fails if those model files are missing.

## Laptop (no hardware)

Needs Python 3.11.

```bash
git clone https://github.com/Crispygazelle/Apex.git
cd Apex
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

```bash
python -m app.main --no-voice                 # simulated ride and dashboard
python -m app.main --no-voice --demo-sprint   # ~90 s highlight, then the arrival card
python -m app.main --no-voice --demo-sprint --simulate-crash 25
                                              # SOS countdown 25 s into the sprint
pytest
```

On the dashboard, **Director** (or the `D` key) can inject a pothole, a hard brake, or a crash rehearsal. **I'm OK — cancel** stops the countdown. Saved rides appear under **Rides** after a run, in `data/rides/`.

Voice, once the models are downloaded:

```bash
pip install -e ".[voice]"
bash scripts/fetch_models.sh
python -m app.main
```

Say **hey apex what's my speed**, **hey apex how far to the destination**, **hey apex log a pothole here**. During a countdown, **cancel** / **I'm okay** — no wake word.

Docker runs that same simulated dashboard, without a virtual environment on the host. Voice models are not in the image.

```bash
docker build -t apex .
docker run --name apex-demo -p 8000:8000 -v "$PWD/data:/out/apex/data" apex
```

The image starts with `--no-voice`. Open http://127.0.0.1:8000/. `docker stop apex-demo` stops it.

---

## Raspberry Pi

```bash
sudo ./deploy/pi-setup.sh
sudo reboot
# after reboot:
i2cdetect -y 1                 # 0x68
timeout 3 cat /dev/ttyAMA0     # $GPRMC / $GNGGA
arecord -l                     # I2S capture device
sudo systemctl start apex
journalctl -u apex -f
```

`deploy/pi-setup.sh` enables I2C / UART / I2S, installs `/opt/apex` with `.[pi,voice]`, fetches models, and enables `deploy/apex.service`. Secrets go in `/etc/apex/apex.env` (see `deploy/apex.env.example`). Boot overlays are in `deploy/boot-config.fragment`.

Manual run on the Pi, from the install at `/opt/apex`:

```bash
source .venv/bin/activate
python -m app.main --backend hardware
```

`--backend hardware` is what makes this a real ride. Without that flag the Pi would simulate, same as the laptop. Do not pass `--demo-sprint` or `--simulate-crash` on the Pi. Those flags only script the simulator.

---

## Voice commands

| You say | It does |
|---|---|
| hey apex what's my speed | Reads fused speed |
| hey apex status report | Speed, g, heading, distance |
| hey apex where am I | Lat/lon if the fix is valid |
| hey apex how far to the destination | Remaining distance along the route |
| hey apex log a pothole here | GPS-tagged hazard, disk + Influx if up |
| cancel / I'm okay | Stops an SOS countdown (no wake word) |

"emergency" is **not** an SOS trigger. Wind-garbled STT is not a reason to page anyone. The detector arms the countdown; voice only cancels it.

This is a phrase list, not a CRF/HMM tokenizer. A visor-down rider has about ten things to say.

---

## Accuracy (synthetic 90 s ride)

Ground truth comes from one `RideSimulator`. Both fake sensors report noisy views of the same trajectory, so these numbers are real error bounds, not "a sample came out".

Measured with `python -m scripts.measure_performance`:

| Measure | Result |
|---|---|
| Fused position | 0.55 m mean, 1.14 m p95, 3.39 m max |
| Dead reckoning, 12 s blackout | 27.6 m mean, 63.4 m max |
| Speed | 0.57 m/s mean error |
| Lean | 0.15° mean error |
| Peak RSS (offline, no Vosk) | 41 MB |
| Battery | **unmeasured** |

Run it on the Pi after install if you want RSS and wall time on 512 MB. Do not quote a battery figure until a real 18650 has done a real ride.

---

## Configuration

`config/apex.yaml` plus environment overlays:

```
APEX__NODE__SENSOR_BACKEND=hardware
APEX__STORAGE__TOKEN=...
APEX__VOICE__ENABLED=true
```

---

## Layout

```
app/
  config.py models.py clock.py geo.py main.py
  sensors/          MPU6050, NEO-M8N, INMP441 + simulated
  processing/       calibration, sync, Kalman, metrics
  pipeline/         acquisition, processor, buffer, coordinator
  storage/          local ride archive, InfluxDB, disk spool
  streaming/        WebSocket hub
  dashboard/        FastAPI cockpit, Dynamics, Rides
  safety/           crash, SOS, status LED
  voice/            wake word, STT, intent, TTS, commands
config/apex.yaml
deploy/             systemd unit, Pi setup, boot overlay
scripts/            fetch_models.sh, fetch_tiles.py, measure_performance.py
Dockerfile          simulated dashboard image (`--no-voice`)
tests/
```

---

## Roadmap (not built)

- Battery characterisation on the 18650 pack
- Cellular path for SOS when Wi-Fi is gone
- Fleet-side hazard aggregation
- Rider-behaviour models
- Mobile app

Crash detection is a corroborated filter, not an ML classifier. Cloud sync is optional InfluxDB, not PostgreSQL.

---

## License

MIT. See the license file if one is present in the repo; otherwise treat it as MIT as declared in `pyproject.toml`.
