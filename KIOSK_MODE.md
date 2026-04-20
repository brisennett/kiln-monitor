# Kiosk Mode

Use this if you want the Raspberry Pi touchscreen to behave like an appliance and open the kiln panel automatically on boot.

This setup:

- boots the Pi into the normal Raspberry Pi OS desktop
- starts Chromium directly on [`/panel`](http://127.0.0.1:8080/panel)
- keeps SSH available for remote admin and recovery
- leaves the underlying OS intact if you ever need it

## What This Uses

- [`kiln-dashboard.service`](/Users/briansennett/Documents/codex/kiln-monitor/kiln-dashboard.service) to serve the UI on port `8080`
- [`launch-kiosk-panel.sh`](/Users/briansennett/Documents/codex/kiln-monitor/launch-kiosk-panel.sh) to configure the display and launch Chromium
- [`kiln-panel-kiosk.service`](/Users/briansennett/Documents/codex/kiln-monitor/kiln-panel-kiosk.service) to start the kiosk on boot

## Assumptions

- Raspberry Pi OS with the desktop installed
- user account is `brisennett`
- project lives at `/home/brisennett/kiln-monitor`
- the dashboard service is already installed and working

If your Pi username or project path is different, update both service files before installing them.

## Install Browser Packages

```bash
sudo apt update
sudo apt install -y chromium-browser unclutter curl
```

If your image uses `chromium` instead of `chromium-browser`, the launcher script will detect that automatically.

## Install The Kiosk Files

From the project directory:

```bash
chmod +x launch-kiosk-panel.sh
sudo cp kiln-panel-kiosk.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable kiln-dashboard.service
sudo systemctl enable kiln-panel-kiosk.service
```

## Auto-Login

The kiosk service expects the Pi to reach the graphical desktop on its own.

Enable desktop auto-login:

```bash
sudo raspi-config
```

Then go to:

- `System Options`
- `Boot / Auto Login`
- `Desktop Autologin`

Reboot after saving.

## Start Or Stop The Kiosk

Start now:

```bash
sudo systemctl start kiln-panel-kiosk.service
```

Stop locally or over SSH:

```bash
sudo systemctl stop kiln-panel-kiosk.service
```

Restart after changes:

```bash
sudo systemctl restart kiln-panel-kiosk.service
```

Check status:

```bash
sudo systemctl status kiln-panel-kiosk.service
```

View logs:

```bash
journalctl -u kiln-panel-kiosk.service -n 100 --no-pager
```

## Remote Admin

Kiosk mode does not remove OS access.

Recommended approach:

- keep SSH enabled
- use the panel locally as the primary touch surface
- do maintenance remotely when needed

That means you can still:

- pull updates
- restart services
- stop the kiosk
- inspect logs
- get to a shell without touching the screen bezel or browser chrome

## Recovery

If the panel does not appear on boot:

1. SSH into the Pi
2. Check the dashboard service:

   ```bash
   sudo systemctl status kiln-dashboard.service
   ```

3. Check the kiosk service:

   ```bash
   sudo systemctl status kiln-panel-kiosk.service
   ```

4. Check the kiosk logs:

   ```bash
   journalctl -u kiln-panel-kiosk.service -n 100 --no-pager
   ```

5. Stop the kiosk if you need the desktop back:

   ```bash
   sudo systemctl stop kiln-panel-kiosk.service
   ```

## Optional Adjustments

You can change the kiosk target page by overriding `KILN_PANEL_URL` in the service file.

Examples:

- `http://127.0.0.1:8080/panel`
- `http://127.0.0.1:8080/alerts`
- `http://127.0.0.1:8080/`

The launcher also uses a dedicated Chromium profile directory so kiosk behavior stays separate from any normal browser usage.
