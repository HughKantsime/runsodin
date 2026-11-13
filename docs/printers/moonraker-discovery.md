# Klipper/Moonraker network discovery

When you add a printer and choose **Klipper / Moonraker**, **Find Klipper printers on the network** lists the Klipper printers on your local network. Click one to fill in the printer address.

Moonraker announces itself over mDNS (zeroconf) as `_moonraker._tcp.local` when its `[zeroconf]` section is enabled in `moonraker.conf` (it is not on by default on every install; without it, type the address instead). O.D.I.N. sends one mDNS query and listens for about three seconds. It never joins the multicast group and keeps no discovery state.

## What you see

Each result shows the printer name, its address (`ip:port`, usually port 7125) and, when the printer answered a status request, the Klipper state (`ready`, `startup`, `error`, `shutdown`). Printers you have already added are marked **Already added** and cannot be picked again. Only printers visible to your organisation count as already added.

## Rules that always apply

- Only addresses on the local network (10.x, 172.16-31.x, 192.168.x) are offered. A public address in an announcement is dropped. This holds in ITAR mode too: the query and the answers stay on the LAN.
- Every address must also pass the printer destination policy (no loopback, link-local, multicast, metadata or reserved addresses, and none of O.D.I.N.'s own service ports).
- A printer is contacted only when it announced its own address. O.D.I.N. then sends one `GET /printer/info` with a 2 second timeout, pinned to that address, with no redirects and no proxy. No API key or other credential is ever sent. A printer that requires authentication shows as not confirmed but can still be picked.
- Discovery needs the **Add printers** permission (administrators by default) and the `write:printers` scope for API tokens. It is limited to 10 scans per minute per client address, and every scan is recorded in the audit log as `printer.discovery` (counts only, no addresses).
- Received packets are parsed defensively: size, record count, name length and compression pointers are all bounded, and malformed packets are ignored.

## When nothing is found

If O.D.I.N. runs in a Docker container on the default bridge network, multicast does not reach your LAN. The list is empty and the dialog says: *Discovery needs the container on the host network or mDNS reflection.* Either run the container with host networking, or run an mDNS reflector (for example Avahi with `enable-reflector=yes`) between the networks. You can always type the printer address by hand.

Discovery only finds IPv4 announcements, and only services that include their address and port in the answer (Moonraker does). A printer on another subnet is found only through a reflector.

## API

`POST /api/printers/discover/moonraker` returns:

```json
{
  "printers": [
    {"name": "Voron", "host": "voron.local", "ip": "192.168.1.50", "port": 7125,
     "address": "192.168.1.50:7125", "already_configured": false,
     "confirmed": true, "klippy_state": "ready", "hostname": "voron"}
  ],
  "reason": null
}
