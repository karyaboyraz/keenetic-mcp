# keenetic-mcp

An [MCP](https://modelcontextprotocol.io) server that lets an AI assistant (Claude, or any MCP client) manage your **Keenetic router** in plain language — list connected devices, pin static DHCP leases, rename devices, check WAN status, and reboot.

It talks to the router's built-in **RCI JSON API** over HTTP, so there's no cloud, no third-party service, and nothing leaves your network. You run it yourself against your own router.

> Tested against a **Keenetic Hopper DSL (KN-3610)**. The RCI API is shared across the Keenetic line (Giga, Viva, Hopper, Ultra, …), so other models should work too. If you confirm one, please open an issue/PR to grow the list.

## Why

Keenetic's RCI API is powerful but poorly documented, and its auth is a fiddly two-step MD5-challenge + SHA256 dance that trips people up for hours. This wraps the common device/lease/WAN operations behind clean MCP tools so you can just say *"pin the Raspberry Pi to a static IP"* and it happens.

## Tools

| Tool | Type | What it does |
|---|---|---|
| `list_devices()` | read | Active DHCP lease table (ip / mac / name / remaining time) |
| `list_static_leases()` | read | Static reservations (`ip dhcp host` in running-config) |
| `find_free_ip(start=100, end=149)` | read | Suggest a free IP for a new static reservation |
| `wan_status()` | read | WAN/internet status, WAN IP, uptime, CPU/memory |
| `list_port_forwards()` | read | Port-forward / static NAT rules (`ip static`) |
| `set_static_lease(mac, ip, name="")` | **write** | Assign a fixed IP to a MAC (low-level — you supply the MAC) |
| `pin_device(identifier, ip="")` | **write** | ⭐ Find a device by IP/MAC/name and pin its current (or a given) IP in one step. Conflict-guarded. |
| `rename_device(identifier, name)` | **write** | ⭐ Set a device's persistent display name (the name shown in the web UI) |
| `remove_static_lease(mac)` | **write** | Remove a reservation |
| `reboot(confirm=True)` | **write** | Reboot the router (only with `confirm=True`; internet drops ~1-2 min) |

### Safety: write tools are off by default

Read tools always work. **Write tools are disabled** until you set `KEENETIC_ENABLE_WRITES=1` in the server environment. This means a fresh install can *look* but not *touch* — no accidental reboots or lease changes while you explore. Even once enabled, write tools are guarded:

- `pin_device` refuses if the target IP is already active on, or statically reserved to, a different device.
- Address-like identifiers (`"66"`) never fuzzy-match a device *name* (e.g. `Room-66-Cam`).
- Every write reads the RCI response back and reports `status:error` instead of silently "succeeding".
- `reboot` requires an explicit `confirm=True`.

## Install

Requires Python 3.9+.

```bash
git clone https://github.com/<you>/keenetic-mcp.git
cd keenetic-mcp
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env
# edit .env: set KEENETIC_PASS (and KEENETIC_ENABLE_WRITES=1 if you want write tools)
```

Run it:

```bash
.venv/bin/python server.py
```

It starts a streamable-HTTP MCP server on `http://0.0.0.0:8905/mcp` (configurable via `KEENETIC_HOST` / `KEENETIC_PORT`).

## Configuration

All configuration is via environment variables (see `.env.example`):

| Variable | Default | Notes |
|---|---|---|
| `KEENETIC_URL` | `http://192.168.1.1` | Your router's web-admin address |
| `KEENETIC_USER` | `admin` | Admin username |
| `KEENETIC_PASS` | — | **Required.** Admin password |
| `KEENETIC_ENABLE_WRITES` | `0` | Set to `1` to enable write tools |
| `KEENETIC_HOST` | `0.0.0.0` | Bind address (use `127.0.0.1` for local-only) |
| `KEENETIC_PORT` | `8905` | Bind port |

## Connecting an MCP client

### Claude Code

Add to your `~/.mcp.json` (or project `.mcp.json`):

```json
{
  "mcpServers": {
    "keenetic": {
      "type": "http",
      "url": "http://127.0.0.1:8905/mcp"
    }
  }
}
```

Then restart Claude Code (MCP config isn't hot-reloaded). Ask: *"list my router's devices"*.

### Claude Desktop / other clients

Point the client at the same streamable-HTTP URL. If your client only supports stdio, run the server with an HTTP-to-stdio bridge, or open an issue — a stdio transport option is easy to add.

## Running as a service (Linux)

A `keenetic-mcp.service` systemd unit is included. Put the repo at `/opt/keenetic-mcp`, create the venv there, then:

```bash
sudo cp keenetic-mcp.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now keenetic-mcp
```

## How the auth works

For anyone reusing the RCI API directly, this is the part that's hard to find. Keenetic uses a two-step challenge:

1. `GET /auth` → returns `401` with `X-NDM-Realm` and `X-NDM-Challenge` headers.
2. Compute `md5 = MD5("<user>:<realm>:<pass>")`, then `sha = SHA256(challenge + md5)`.
3. `POST /auth` with `{"login": user, "password": sha}` → sets a session cookie.
4. `POST /rci/` with a `list[dict]` command tree, using that cookie.

Write commands must be followed by `[{"system":{"configuration":{"save":{}}}}]` or they're lost on reboot. See `server.py` for the full, working implementation.

## Security notes

- The server has **full admin control** of your router. Bind it to `127.0.0.1` (or a trusted LAN only) — never expose port 8905 to the internet.
- Your password lives only in `.env` (gitignored). Nothing is sent anywhere except your own router.
- This is not affiliated with or endorsed by Keenetic. Use at your own risk.

## License

MIT — see [LICENSE](LICENSE).
