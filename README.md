# mi_temp_reporter

A robust, unattended temperature and humidity reporter for the Xiaomi LYWSD03MMC sensor (running [ATC/pvvx custom firmware]([https://github.com/pvvx/ATC_MiThermometer]) in passive BLE mode), deployed on a Raspberry Pi.

Every hour (configurable) the script:

1. Spawns MiTemperature2 for a short BLE scan window
2. Collects the sensor reading via a shell callback
3. POSTs the reading as JSON to a configurable HTTP API endpoint
4. Sleeps until the next cycle

## Installation

### Pre-requisites

- Python 3.6+
- [JsBergbau/MiTemperature2](https://github.com/JsBergbau/MiTemperature2)
- Xiaomi LYWSD03MMC sensor (running [ATC/pvvx firmware](https://github.com/pvvx/ATC_MiThermometer))

### Clone

```bash
git clone https://github.com/mabenj/mi_temp_reporter.git
cd mi_temp_reporter
```

### Make script executable

```bash
chmod +x mi_temp_reporter.py
```

### Update configuration

Edit `mi_temp_reporter.conf` with your configuration. Make sure that `mitemp_script` points to the MiTemperature2.py script and that `api_url` points to your API endpoint.

All configuration can also be overridden via environment variables named `MI_TEMP_<KEY>`.

Remember to also update the sensors.ini file with the MAC address of your sensor.

### Systemd service

Install and enable:

```bash
cp mi_temp_reporter.service /etc/systemd/system/mi_temp_reporter.service
sudo systemctl daemon-reload
sudo systemctl enable mi_temp_reporter.service
sudo systemctl start mi_temp_reporter.service
```

Useful commands:

```bash
sudo systemctl status mi_temp_reporter.service
sudo systemctl stop mi_temp_reporter.service
sudo systemctl restart mi_temp_reporter.service
journalctl -u mi_temp_reporter.service -f
journalctl -u mi_temp_reporter-service --since "1 hour ago"
```

## API payload format

```json
{
  "sensorname": "AA:BB:CC:DD:EE:FF",
  "temperature": "21.3",
  "humidity": "45.0",
  "voltage": "2.98",
  "batteryLevel": "82",
  "timestamp": "1772831278",
  "reportedAt": "2026-03-21T12:00:00.123456+00:00"
}
```

## Daily reboot (optional)

For a Pi that cannot be physically accessed, a daily reboot clears any accumulated BLE or OS state:

```bash
sudo crontab -e
```

Add the following line:

```
# Reboot daily at 03:00
0 3 * * * /sbin/reboot
```

## Troubleshooting

### Bluetooth not working

```bash
rfkill
rfkill unblock bluetooth
```
