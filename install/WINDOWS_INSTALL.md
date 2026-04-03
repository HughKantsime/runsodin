# O.D.I.N. Windows Installation Guide

## Prerequisites

1. **Windows 10 (1903+) or Windows 11**
2. **Docker Desktop for Windows** — [Download here](https://www.docker.com/products/docker-desktop/)
3. **WSL2** — Required for Docker Desktop on Windows Home. Run `wsl --install` in PowerShell if not already enabled.

## Installation

### Option A: One-command installer (recommended)

Open PowerShell and run:

```powershell
irm https://raw.githubusercontent.com/HughKantsime/runsodin/master/install/install.ps1 | iex
```

If you get an execution policy error, first run:

```powershell
Set-ExecutionPolicy RemoteSigned -Scope CurrentUser
```

### Option B: Manual install

```powershell
mkdir C:\odin; cd C:\odin
Invoke-WebRequest -Uri "https://raw.githubusercontent.com/HughKantsime/runsodin/master/install/docker-compose.yml" -OutFile docker-compose.yml
docker compose up -d
```

Open `http://localhost:8000` and follow the setup wizard.

## After Installation

- **Access ODIN:** Open `http://localhost:8000` (or `http://your-ip:8000` from other devices)
- **First-time setup:** Create your admin account in the browser
- **Data location:** `C:\odin\odin-data\` (or `%USERPROFILE%\odin\odin-data\` if installed without admin)

## Updating

Open PowerShell in your install directory and run:

```powershell
docker compose pull
docker compose up -d
```

## Firewall Configuration

If other devices on your network can't reach ODIN, you may need to open port 8000 in Windows Firewall.

Run as Administrator:

```powershell
netsh advfirewall firewall add rule name="ODIN Web" dir=in action=allow protocol=tcp localport=8000
netsh advfirewall firewall add rule name="ODIN go2rtc" dir=in action=allow protocol=tcp localport=1984
netsh advfirewall firewall add rule name="ODIN WebRTC" dir=in action=allow protocol=tcp localport=8555
```

## Uninstalling

```powershell
cd C:\odin
docker compose down -v
cd ..
Remove-Item -Recurse -Force C:\odin
```

This removes the container, volumes, and all data. Remove the firewall rules if you added them:

```powershell
netsh advfirewall firewall delete rule name="ODIN Web"
netsh advfirewall firewall delete rule name="ODIN go2rtc"
netsh advfirewall firewall delete rule name="ODIN WebRTC"
```

## Troubleshooting

### Docker Desktop won't start
- Ensure WSL2 is installed: `wsl --install`
- Restart your computer after installing WSL2
- Check that virtualization is enabled in BIOS/UEFI

### "Port already in use" error
Another application is using port 8000, 1984, or 8555. Find it:

```powershell
Get-NetTCPConnection -LocalPort 8000 -State Listen | Select-Object OwningProcess
```

### Container starts but ODIN doesn't load
Check the container logs:

```powershell
docker compose logs -f
```

### Permission denied errors
Run PowerShell as Administrator, or install to `%USERPROFILE%\odin` instead of `C:\odin`.

### WSL2 not working
```powershell
wsl --update
wsl --set-default-version 2
```

If WSL2 keeps failing, ensure "Virtual Machine Platform" and "Windows Subsystem for Linux" are enabled in Windows Features.
