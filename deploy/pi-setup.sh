#!/usr/bin/env bash
# First-boot bring-up for a Raspberry Pi Zero 2 W running APEX.
# Run as root from a clone of the repo. Safe to re-run.

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
INSTALL_ROOT="${APEX_ROOT:-/opt/apex}"
SERVICE_USER="${APEX_USER:-apex}"

die() { echo "error: $*" >&2; exit 1; }

[[ "$(id -u)" -eq 0 ]] || die "run as root (sudo $0)"

if [[ ! -e /proc/device-tree/model ]] || ! grep -qi "raspberry pi" /proc/device-tree/model; then
  die "this is not a Raspberry Pi; refusing to change boot config"
fi

echo "==> Enabling I2C, UART (no console), and I2S"
if command -v raspi-config >/dev/null; then
  raspi-config nonint do_i2c 0
  # 2 = serial port on, login shell off. Needed so /dev/ttyAMA0 is the GPS.
  raspi-config nonint do_serial 2 || raspi-config nonint do_serial_hw 0
else
  echo "    raspi-config missing; apply deploy/boot-config.fragment by hand"
fi

BOOT_CONFIG=""
for candidate in /boot/firmware/config.txt /boot/config.txt; do
  if [[ -f "$candidate" ]]; then
    BOOT_CONFIG="$candidate"
    break
  fi
done
[[ -n "$BOOT_CONFIG" ]] || die "could not find config.txt"

FRAGMENT="$REPO/deploy/boot-config.fragment"
if ! grep -q "dtoverlay=googlevoicehat-soundcard" "$BOOT_CONFIG"; then
  echo "==> Appending I2S overlay to $BOOT_CONFIG"
  {
    echo ""
    echo "# --- APEX $(date -I) ---"
    grep -v '^#' "$FRAGMENT" | grep -v '^$'
  } >> "$BOOT_CONFIG"
else
  echo "==> Boot overlay already present"
fi

if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  echo "==> Creating user $SERVICE_USER"
  useradd --system --home-dir "$INSTALL_ROOT" --create-home \
    --shell /usr/sbin/nologin --groups i2c,dialout,audio,gpio "$SERVICE_USER"
else
  usermod -aG i2c,dialout,audio,gpio "$SERVICE_USER" || true
fi

echo "==> Installing the tree at $INSTALL_ROOT"
mkdir -p "$INSTALL_ROOT"
if [[ "$REPO" != "$INSTALL_ROOT" ]]; then
  rsync -a --delete \
    --exclude .git --exclude .venv --exclude venv --exclude data --exclude models \
    "$REPO"/ "$INSTALL_ROOT"/
fi

if [[ ! -x "$INSTALL_ROOT/.venv/bin/python" ]]; then
  echo "==> Creating Python 3.11 venv (Bookworm system python3 is 3.11)"
  python3 -m venv "$INSTALL_ROOT/.venv"
fi

echo "==> Installing package extras: base + pi + voice"
"$INSTALL_ROOT/.venv/bin/pip" install -U pip
"$INSTALL_ROOT/.venv/bin/pip" install -e "$INSTALL_ROOT[pi,voice]"

echo "==> Fetching speech models"
sudo -u "$SERVICE_USER" bash "$INSTALL_ROOT/scripts/fetch_models.sh" || \
  bash "$INSTALL_ROOT/scripts/fetch_models.sh"
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_ROOT"

echo "==> Installing systemd unit"
mkdir -p /etc/apex
if [[ ! -f /etc/apex/apex.env ]]; then
  cp "$INSTALL_ROOT/deploy/apex.env.example" /etc/apex/apex.env
  chmod 600 /etc/apex/apex.env
fi
cp "$INSTALL_ROOT/deploy/apex.service" /etc/systemd/system/apex.service
systemctl daemon-reload
systemctl enable apex.service

echo
echo "Bring-up is not finished until you reboot and check the sensors:"
echo "  sudo reboot"
echo "  i2cdetect -y 1          # expect 0x68"
echo "  timeout 3 cat /dev/ttyAMA0 | head   # expect NMEA (\$GPRMC / \$GNGGA)"
echo "  arecord -l              # expect an I2S capture device"
echo "  sudo systemctl start apex"
echo "  journalctl -u apex -f"
