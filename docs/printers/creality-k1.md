# Creality K1, K1 Max and K1C with O.D.I.N.

**Support level: Experimental (not verified on hardware by the O.D.I.N. team).** The K1 family runs Klipper. O.D.I.N. talks to Klipper printers through Moonraker, so a K1 works with O.D.I.N.'s existing Klipper / Moonraker printer type once Moonraker is reachable on your network. Nothing in O.D.I.N. is specific to Creality.

## What you need
- A K1, K1 Max or K1C on your local network with a fixed IP address (reserve it in your router).
- Moonraker reachable from the O.D.I.N. server. Stock Creality firmware does not expose Moonraker to the network. Owners enable it by turning on root access in the printer's settings and installing Moonraker with a community tool. That step is outside O.D.I.N., changes the printer's firmware behaviour and is at your own risk; follow the tool's own instructions and check Creality's warranty terms first.
- The Moonraker port. Community installs on the K1 family commonly use port **7125**; some builds serve the web interface on 4408 or 4409 and Moonraker behind it. If 7125 does not answer, check your install's notes.

## Add it to O.D.I.N.
1. In **Printers > Add Printer**, choose **Klipper / Moonraker**.
2. Press **Find Klipper printers on the network** (O.D.I.N. 2.2.0 and later). If the printer announces itself, pick it from the list. If nothing is found, type the address yourself as `IP:port`, for example `192.168.1.50:7125`. Discovery needs the O.D.I.N. container to receive multicast (host networking or an mDNS reflector); typing the address always works.
3. Leave the API key empty unless your Moonraker requires one.
4. Press **Test connection**. A reply from `/printer/info` means O.D.I.N. can monitor the printer.

## What works
Whatever O.D.I.N.'s Klipper / Moonraker support does on any Klipper printer: status, temperatures, progress, pause, resume, stop and job sending of `.gcode` files. O.D.I.N. does not slice: send G-code sliced for your printer.

## What to check and tell us
We have not run a K1 against O.D.I.N. If you do, the things most worth reporting are: whether discovery finds it, which port your install uses, whether pause/resume/stop behave, and whether the camera stream URL Moonraker reports works in O.D.I.N. Use **Settings > System > Troubleshooting > Prepare a report** and send it to support.

## Not supported
- Creality Print's cloud features and Creality's own LAN protocol: O.D.I.N. uses Moonraker only.
- Anything that needs the printer to stay on stock, unrooted firmware.
