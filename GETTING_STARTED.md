# Getting Started with PrintFarm Scheduler

This guide walks you through setting up PrintFarm Scheduler from scratch.

## Option 1: Quick Local Test (No Docker)

Best for: Testing the backend works before committing to a full deployment.

### Prerequisites
- Python 3.10+ installed
- ~10 minutes

### Steps

```bash
# 1. Clone/copy the project
cd /path/to/printfarm-scheduler

# 2. Run the setup script (creates venv, installs deps)
./setup-dev.sh

# 3. Start the backend
cd backend
source venv/bin/activate
uvicorn main:app --reload

# 4. Open the API docs
# Visit: http://localhost:8000/docs
```

You can now:
- Create printers via the API
- Add jobs
- Run the scheduler
- See the timeline

The frontend won't work yet (needs `npm install`), but you can test all the core functionality via the Swagger UI at `/docs`.

---

## Option 2: Docker Deployment (Recommended for Homelab)

Best for: Running in production on your Proxmox homelab.

### Prerequisites
- Docker & Docker Compose installed
- ~5 minutes

### Steps

```bash
# 1. Clone/copy the project to your server
scp -r printfarm-scheduler user@your-server:/opt/

# 2. SSH in and navigate to the directory
ssh user@your-server
cd /opt/printfarm-scheduler

# 3. (Optional) Configure environment
cp .env.example .env
nano .env  # Edit settings if needed

# 4. Start everything
docker-compose up -d

# 5. Check it's running
docker-compose ps
docker-compose logs -f
```

Access:
- **Frontend**: http://your-server:8080
- **API**: http://your-server:8000
- **API Docs**: http://your-server:8000/docs

---

## Option 3: LXC Container on Proxmox

Best for: Isolated deployment without full Docker overhead.

### Steps

1. **Create an LXC container** in Proxmox:
   - Template: Ubuntu 22.04 or Debian 12
   - RAM: 512MB+
   - Disk: 4GB+
   - Enable nesting if you want Docker inside

2. **Install dependencies**:
```bash
apt update && apt install -y python3 python3-pip python3-venv git

# Optional: Install Node.js for frontend
curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
apt install -y nodejs
```

3. **Clone and setup**:
```bash
cd /opt
git clone <your-repo> printfarm-scheduler
cd printfarm-scheduler
./setup-dev.sh
```

4. **Create a systemd service**:
```bash
cat > /etc/systemd/system/printfarm.service << 'EOF'
[Unit]
Description=PrintFarm Scheduler API
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/printfarm-scheduler/backend
Environment=PATH=/opt/printfarm-scheduler/backend/venv/bin
ExecStart=/opt/printfarm-scheduler/backend/venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable printfarm
systemctl start printfarm
```

---

## Importing Your Google Sheets Data

Once the backend is running, you can import your existing data:

### 1. Export CSVs from Google Sheets

In your Google Sheets workbook:
- **File → Download → Comma Separated Values (.csv)**
- Export each sheet separately:
  - `PrinterConfig` → `printers.csv`
  - `Pricing` → `models.csv`
  - `Jobs` → `jobs.csv`

### 2. Copy CSVs to your server

```bash
scp printers.csv models.csv jobs.csv user@server:/opt/printfarm-scheduler/
```

### 3. Run the import script

```bash
cd /opt/printfarm-scheduler

# Import everything at once
python3 import_from_sheets.py \
  --printers printers.csv \
  --models models.csv \
  --jobs jobs.csv

# Or import individually
python3 import_from_sheets.py --printers printers.csv
python3 import_from_sheets.py --models models.csv
python3 import_from_sheets.py --jobs jobs.csv
```

### 4. Verify the import

Visit `http://your-server:8000/docs` and try:
- `GET /api/printers` - should show your printers
- `GET /api/models` - should show your models
- `GET /api/jobs` - should show your jobs

---

## Connecting to Spoolman

If you have Spoolman running:

1. **Find your Spoolman URL** (e.g., `http://192.168.1.50:7912`)

2. **Set the environment variable**:
```bash
# In .env file:
SPOOLMAN_URL=http://192.168.1.50:7912

# Or in docker-compose.yml:
environment:
  - SPOOLMAN_URL=http://192.168.1.50:7912
```

3. **Restart the service**:
```bash
docker-compose restart
# or
systemctl restart printfarm
```

4. **Sync spools**: 
   - Visit `/docs` 
   - Try `POST /api/spoolman/sync`
   - Or `GET /api/spoolman/spools` to see available spools

---

## Common Tasks

### Run the Scheduler

```bash
# Via API
curl -X POST http://localhost:8000/api/scheduler/run

# Or use the UI dashboard "Run Scheduler" button
```

### Add a Quick Job via CLI

```bash
curl -X POST http://localhost:8000/api/jobs \
  -H "Content-Type: application/json" \
  -d '{
    "item_name": "Baby Yoda (flexi)",
    "priority": 2,
    "duration_hours": 16.5,
    "colors_required": "black, white, caramel matte, lime green matte"
  }'
```

### Check System Status

```bash
curl http://localhost:8000/api/stats
```

---

## Troubleshooting

### Backend won't start
```bash
# Check logs
docker-compose logs backend
# or
journalctl -u printfarm -f
```

### Database issues
```bash
# Reset the database (WARNING: deletes all data)
rm backend/printfarm.db
# Restart - tables will be recreated
```

### Import script errors
- Make sure CSV columns match expected names
- Check for special characters in the CSV
- Try importing one file at a time to isolate issues

### Can't connect to Spoolman
- Verify Spoolman URL is reachable: `curl http://your-spoolman:7912/api/v1/health`
- Check firewall rules between containers/hosts
- Make sure they're on the same Docker network if using Docker

---

## Next Steps

Once you have it running:

1. **Add your printers** via the UI or API
2. **Import or create models** for your frequently-printed items
3. **Add jobs to the queue**
4. **Run the scheduler** to assign jobs to printers
5. **Use the timeline** to visualize your print schedule

For feature requests or bugs, the code is yours to modify!
