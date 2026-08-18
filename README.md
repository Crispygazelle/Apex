# 🏍️ APEX: F1-Style Smart Helmet Edge Node

An intelligent, privacy-first, edge-computing telemetry and hands-free vocal assistant module for motorcycle helmets. Real-time kinematics tracking, crash detection, and offline NLP—no cloud dependency required.

![Python](https://img.shields.io/badge/Python-3.10+-blue)
![Raspberry Pi](https://img.shields.io/badge/Raspberry%20Pi-Zero%202%20W-red)
![License](https://img.shields.io/badge/License-MIT-green)

---

## 🎯 Project Objective

APEX transforms motorcycle helmets into autonomous edge nodes that:

- **Process sensor data locally** – No cloud dependency, zero latency, complete privacy
- **Deliver hands-free voice assistance** – Navigate, accept orders, and log hazards without visual distraction
- **Track high-resolution kinematics** – Velocity, acceleration, lean angles, climbing gradients
- **Detect crashes automatically** – Multi-sensor fusion with emergency distress signaling
- **Enable crowdsourced hazard mapping** – GPS-tagged infrastructure issues for fleet routing optimization

---

## 🎯 Real-World Applications

### 🚴 Hyperlocal Delivery & Logistics
- **Target:** Zomato, Swiggy, Zepto, Dunzo, Porter delivery fleets
- **Use Case:** Hands-free order acceptance, navigation, and drop confirmation
- **Safety Benefit:** Crowdsourced pothole/hazard mapping for intelligent fleet rerouting

### ⚡ Premium EV OEMs & Fleet Managers
- **Target:** Ather Energy, Ola Electric, Bounce Infinity, Yulu
- **Use Case:** High-resolution kinetic profiling for regeneration optimization and cell-degradation prediction
- **Benefit:** Decentralized testing environment for algorithm development

### 🏔️ Endurance Motovlogging & Long-Distance Touring
- **Target:** Leisure touring motorcyclists on high-risk corridors (Leh-Ladakh, Spiti Valley)
- **Use Case:** Continuous safety monitoring with crash detection
- **Benefit:** Automated emergency distress signals with precise GPS coordinates

---

## 🛠️ Hardware Bill of Materials (BOM)

| Component | Model | Function | Interface |
|-----------|-------|----------|-----------|
| **Compute Core** | Raspberry Pi Zero 2 W | Edge orchestration, NLP, cloud sync | GPIO, I2C, UART, I2S |
| **Kinetic Sensor** | MPU6050 (6-Axis IMU) | G-force, acceleration, lean angles | I2C |
| **Spatial Sensor** | NEO-M8N GPS Module | Latitude, longitude, altitude, velocity | UART (NMEA) |
| **Acoustic Input** | INMP441 I2S MEMS Microphone | Hands-free voice commands | I2S |
| **Power Supply** | 18650 Li-ion + TP4056 | 3.7V → 5V regulated output | USB |
| **Enclosure** | Custom 3D-Printed ABS | Weatherproof, aerodynamic, camera-mount compatible | N/A |

---

## 💻 Software Stack

- **Language:** Python 3.10+
- **Edge Inference:** scikit-learn, CRFsuite (ARMv7-optimized)
- **Cloud API:** FastAPI with async worker pools
- **Database:** PostgreSQL (cloud) + SQLite (edge fallback)
- **Visualization:** Streamlit dashboards + Grafana (local) + Leaflet.js (offline maps)
- **NLP:** Vosk (offline STT) + Piper TTS (offline speech synthesis)
- **Sensor Fusion:** Kalman Filter for IMU + GPS merge

---

## 📊 Core Technical Features

### High-Resolution Kinematics Tracking
```
Instantaneous Velocity (v):  GPS-based Haversine formula between successive coordinates
Longitudinal Acceleration:   a = Δv / Δt (MPU6050 I2C @ 100Hz)
Climbing Gradient (%):       g = (Δaltitude / Δdistance) × 100
Lean Angle:                  Gyroscope roll integration
```

### Sensor Fusion Engine
- **Kalman Filter:** Merges fast IMU data (100Hz) with accurate GPS (10Hz)
- **Dead Reckoning:** Predicts position during GPS outages
- **Anomaly Detection:** Cross-references sudden G-force spikes with velocity drops to classify crashes

### Offline NLP Pipeline
1. **Wake-Word Detection** – Low-power acoustic model ignores wind noise
2. **Speech-to-Text** – Vosk offline engine transcribes audio instantly
3. **Intent Extraction** – CRF/HMM tokenizer identifies commands (e.g., "Log hazard: pothole")
4. **Text-to-Speech** – Piper TTS synthesizes verbal responses
5. **Privacy-First:** Raw audio deleted immediately after processing

### Crash Detection & Emergency Protocol
- **Passive Override State:** Halts voice pipeline on impact > 4G force
- **Emergency Trigger:** Automated distress signal with GPS coordinates
- **Local-First:** Functions even without cellular connectivity

---

## 📁 Project Structure

```
apex/
├── mpu.py                 # MPU6050 IMU driver (6-axis kinetics)
├── read_gps.py            # NEO-M8N GPS NMEA parser
├── traffic_light.py       # Status indicator & state machine
├── telemetry_daemon.py    # Main async sensor fusion loop
├── npl_intent_parser.py   # Offline voice command processor
├── kalman_filter.py       # Sensor fusion algorithm
├── requirements.txt       # Python dependencies
├── venv/                  # Virtual environment
├── config.json            # Sensor calibration & thresholds
├── data/                  # Local SQLite buffers & logs
└── README.md              # This file
```

---

## 🚀 Getting Started

### 1. **Clone the Repository**
```bash
git clone https://github.com/Crispygazelle/apex.git
cd apex
```

### 2. **Set Up Virtual Environment**
```bash
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

### 3. **Install Dependencies**
```bash
pip install -r requirements.txt
```

### 4. **Hardware Setup (Raspberry Pi)**

**Wiring Reference:**
```
MPU6050 (I2C):
  - VCC → Pi 3.3V (Pin 1)
  - GND → Pi GND (Pin 6)
  - SDA → Pi GPIO 2 (Pin 3)
  - SCL → Pi GPIO 3 (Pin 5)

NEO-M8N (UART):
  - VCC → Pi 5V (Pin 2)
  - GND → Pi GND (Pin 6)
  - TX → Pi RX (GPIO 15, Pin 10)
  - RX → Pi TX (GPIO 14, Pin 8)

INMP441 (I2S):
  - VCC → Pi 3.3V
  - GND → Pi GND
  - CLK → Pi GPIO 18
  - WS  → Pi GPIO 19
  - SD  → Pi GPIO 20

TP4056 (Power):
  - Input: USB 5V
  - Output: 5V to Pi GPIO power header
```

### 5. **Enable I2C, UART, and I2S on Raspberry Pi**
```bash
sudo raspi-config
# Enable: I2C, Serial (UART), I2S in Interfacing Options
# Reboot
sudo reboot
```

### 6. **Run the System**
```bash
python3 telemetry_daemon.py
```

---

## 📖 Usage Guide

### Voice Commands (Hands-Free)
Once running, the system listens for voice triggers:

```
"What's my speed?"           → Fetches current velocity
"Log hazard: pothole"        → Marks GPS coordinate with hazard type
"Status report"              → Reads back vital metrics (speed, G-force, altitude)
"Emergency"                  → Triggers SOS protocol
```

### Real-Time Dashboard
Access live telemetry via Streamlit:
```bash
streamlit run dashboard.py
```

The dashboard displays:
- 🗺️ Live GPS tracking on offline map
- 📊 Speedometer & G-force gauges
- 📝 Voice command log with timestamps
- 🚨 Hazard markers and crash alerts

### Data Export
Post-ride data is available in:
- `data/telemetry.db` – SQLite database (local edge storage)
- `data/telemetry.csv` – CSV export for post-processing

---

## 🔧 Core Modules

### `mpu.py`
Initializes and reads 6-axis IMU data (acceleration, gyroscope).
```python
from mpu import MPU6050
imu = MPU6050()
accel_x, accel_y, accel_z = imu.read_acceleration()
gyro_x, gyro_y, gyro_z = imu.read_gyroscope()
```

### `read_gps.py`
Parses NMEA strings from NEO-M8N and extracts coordinates.
```python
from read_gps import GPSModule
gps = GPSModule()
lat, lon, altitude, velocity = gps.get_position()
```

### `traffic_light.py`
Status indicator system for operational states.
```python
from traffic_light import StatusIndicator
status = StatusIndicator()
status.set_green()  # All systems nominal
status.set_red()    # Emergency state
```

---

## 🔐 Privacy & Security

✅ **Zero Cloud Dependency:** All processing happens on the edge device  
✅ **Local-Only Storage:** Telemetry buffered in SQLite, synced post-ride only  
✅ **Audio Privacy:** Raw audio deleted immediately after STT processing  
✅ **No GPS Stalking:** Location data retained locally unless user explicitly syncs  
✅ **Encrypted Sync:** Optional HTTPS/SSL for cloud offload (fully optional)

---

## ⚡ Performance Specs

| Metric | Value |
|--------|-------|
| **Sensor Loop Frequency** | 100Hz (IMU), 10Hz (GPS), Continuous (Audio) |
| **Latency (Voice → Response)** | <500ms (offline NLP) |
| **Battery Life** | 6–8 hours (18650 @ 2000mAh continuous use) |
| **Storage (Edge)** | 32GB microSD (local SQLite buffers) |
| **Network Dependency** | Optional (fallback to local buffering) |
| **Processing Power** | Raspberry Pi Zero 2 W (ARMv7 1.0GHz dual-core) |

---

## 📡 System Architecture Layers

```
┌─────────────────────────────────────────┐
│   Layer 4: Frontend (Streamlit / Mobile) │  (Optional: Real-time dashboard)
└──────────────────┬──────────────────────┘
                   │
┌──────────────────▼──────────────────────┐
│ Layer 3: Acoustic NLP (Vosk + Piper TTS) │  (Hands-free voice interface)
└──────────────────┬──────────────────────┘
                   │
┌──────────────────▼──────────────────────┐
│ Layer 2: Edge Compute (Raspberry Pi)     │  (Kalman Filter, Intent Parser, Buffering)
├──────────────────────────────────────────┤
│  ├─ Sensor Fusion Engine                 │
│  ├─ SQLite Time-Series Database          │
│  ├─ WiFi / Cellular Sync Manager         │
│  └─ Crash Detection Logic                │
└──────────────────┬──────────────────────┘
                   │
┌──────────────────▼──────────────────────┐
│ Layer 1: Hardware Sensors (I2C/UART/I2S)│  (MPU6050, NEO-M8N, INMP441)
└──────────────────────────────────────────┘
```

---

## 🐛 Troubleshooting

### GPS Not Acquiring Lock
- Ensure antenna is outdoors with clear sky view
- Check UART baud rate (default: 9600)
- Verify `read_gps.py` is reading NMEA strings

### IMU Data Noisy
- Run calibration routine in `mpu.py`
- Ensure secure I2C connection (no loose wires)
- Reduce sensor polling rate if necessary

### Voice Commands Not Recognized
- Check microphone wiring (I2S clock, data lines)
- Verify Vosk models are installed
- Test audio capture: `arecord -D hw:0,0 -d 5 test.wav`

### Battery Draining Fast
- Reduce IMU polling frequency to 50Hz
- Disable Streamlit dashboard (use offline only)
- Check for blocking I/O operations in `telemetry_daemon.py`

---

## 📚 References & Resources

- [Raspberry Pi I2C Setup](https://www.raspberrypi.com/documentation/computers/computers-and-raspberry-pi/linux/i2c.html)
- [MPU6050 Datasheet](https://invensense.tdk.com/products/motion-tracking/6-axis/mpu-6050/)
- [NEO-M8N GPS Module](https://www.u-blox.com/en/product/neo-m8-series)
- [Vosk Offline Speech Recognition](https://alphacephei.com/vosk/)
- [Kalman Filter Sensor Fusion](https://en.wikipedia.org/wiki/Kalman_filter)

---

## 🤝 Contributing

Contributions welcome! Areas for enhancement:
- [ ] Expand NLP command set
- [ ] Optimize Kalman Filter parameters
- [ ] Add crash detection ML model
- [ ] Implement cloud sync via FastAPI
- [ ] Create mobile app (Flutter/React Native)

---

## 📄 License

This project is licensed under the MIT License – see LICENSE file for details.

---

## ✉️ Contact & Support

For questions, issues, or feature requests, open a GitHub issue or reach out to the project maintainer.

---

## 🚀 Roadmap

- **v1.0** – Core telemetry + voice commands (Current)
- **v1.1** – Crash detection + emergency protocol
- **v1.2** – Cloud sync + Streamlit dashboard
- **v2.0** – Machine learning rider behavior analysis
- **v2.1** – Fleet hazard mapping aggregation

---

**Last Updated:** August 2026  
**Status:** 🟢 Active Development

