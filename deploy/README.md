# Pi deployment

| File | What it is |
|---|---|
| `pi-setup.sh` | First-boot: overlays, venv, extras, models, systemd |
| `apex.service` | Installed to `/etc/systemd/system/apex.service` |
| `apex.env.example` | Copied to `/etc/apex/apex.env` (mode 600) |
| `boot-config.fragment` | I2C / UART / I2S lines appended to `config.txt` |

Run `sudo ./deploy/pi-setup.sh` from a clone on the Pi, then reboot and check the sensors as the script prints. The unit starts `python -m app.main --backend hardware --quiet`.

This script refuses to run on a machine that is not a Raspberry Pi.
