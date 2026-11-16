# splitflap-web

A web app for the Raspberry Pi that controls a split-flap display built from
[g000ze/Split-Flap-Display](https://github.com/g000ze/Split-Flap-Display).
It replaces the PHP control layer with Python (standard library only, nothing
to `pip install`) and talks to the Arduino boards over I²C with the same
protocol, so the firmware stays as it is.

- Phone-first UI that also works on tablets and computers, with a live mirror of the display
- All the stagger animations of the PHP version (from left/right, land together, random, snakes …)
- **Clock** and **Temperature** buttons (current temperature for a place you set in the app,
  from [Open-Meteo](https://open-meteo.com), no API key). The display keeps showing them,
  and after a reboot it comes back in the same mode
- Favourites, recent messages, resync (one full turn so the Hall sensors re-home)
- Reachable on the same Wi-Fi at `http://<hostname>.local`
- A JSON API and a CLI for scripts (flights overhead, now playing, …)
- A built-in simulator, so you can try it on a laptop without the display

## Install on the Pi

Needs Raspberry Pi OS with Python 3.7 or newer (any current image).

```bash
git clone https://github.com/SandroArrigoni/splitflap-web.git
cd splitflap-web
./install.sh                         # or: ./install.sh --hostname splitflap
```

The script enables I²C, creates `config.json`, makes sure mDNS (avahi) is running,
installs a systemd service that starts on boot and prints the address, e.g.
`http://splitflap.local`. It uses port 80 if it is free; if the old PHP web
server is still on port 80 it falls back to `:8080`.

Update later with `./update.sh` (git pull + restart).

## Configure

Edit `config.json` (not tracked by git, so updates never overwrite it) and
restart with `sudo systemctl restart splitflap-web`.

```json
"layout": [
  [ { "address": "0x0a", "modules": 6 } ]
]
```

`layout` is a list of rows; each row lists its Arduino boards from left to
right with their I²C address and number of modules. The 1×6 prototype is one
board. The planned 3×16 display is three rows of two 8-module boards
(`0x0a`–`0x0f`, as in the original PHP `$targets`).

Don't know the address? `python3 app.py scan` lists every board that answers
(or open **Hardware → Scan I²C bus** in the app).

`temperature.format` sets how it looks on the flaps (`{temp}C` → `-3C`; there is no ° flap),
`refresh_minutes` how often it updates, `location` the default place before you set one in the app.

`charset` and `motor` must match the firmware (`char_positions[]`, `flaps`,
`microsteps`, `motorsteps`, `speed`). The defaults match the current
`Split_Flap.ino`.

## Command line

```bash
python3 app.py send "HELLO" -a left      # like the old i2c_transmit.php
python3 app.py send "ZURICH" -a stop --wipe
python3 app.py clear
python3 app.py status                    # what every module shows right now
python3 app.py scan
python3 app.py --simulate                # web app with the simulator, http://localhost:8080
```

Stop the service first (`sudo systemctl stop splitflap-web`) if you want to run
the web app by hand on the same port.

## API

| Method | Path | Body / query |
|---|---|---|
| GET | `/api/state` | modules, hardware status, presets, history |
| POST or GET | `/api/display` | `text` or `lines` (list per row), `animation`, `same`, `wipe`, `slowdown` (1–10) |
| POST | `/api/clear` | `animation` |
| POST | `/api/resync` | – |
| POST | `/api/stop` | – |
| POST | `/api/mode` | `mode`: `text` / `clock` / `temperature`, `format` (clock, strftime, e.g. `%H:%M`) |
| POST | `/api/location` | `query`: a place name (`Zürich`) or `lat, lon` |
| GET | `/api/scan` | – |

```bash
curl "http://splitflap.local/api/display?text=HALLO&animation=left&wipe=1"
```

Animations: `start` (all at once), `stop` (land together), `left`, `right`,
`random`, and for multi-row grids `top`, `bottom`, `diagonal`, `cross`,
`round`, `snake1`–`snake4`.

## Layout of the code

```
app.py                  entry point: web app + CLI
splitflap/display.py    port of split-flap.php (sanitising, grid, animations, timed send)
splitflap/bus.py        /dev/i2c-N access via ioctl, and the simulator
splitflap/controller.py one job at a time, status cache, clock/temperature modes, favourites
splitflap/weather.py    Open-Meteo place search and current temperature
splitflap/server.py     HTTP server and JSON API
static/                 the web UI (no build step, no CDN)
```

Characters that are not on the flaps (e.g. `&`, `%`) leave that module as it
is, like in the PHP version; the app marks them in red before you send.
Umlauts are folded (`Zürich` → `ZURICH`).
