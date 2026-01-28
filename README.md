# PrintFarm Scheduler

A self-hosted job scheduler for 3D print farms that optimizes printer utilization by intelligently batching jobs based on loaded filaments.

## Features

- **Smart Job Scheduling**: Automatically assigns jobs to printers based on color matching to minimize filament changes
- **Timeline View**: Visual calendar/Gantt view of scheduled print jobs across your farm
- **Filament State Tracking**: Knows what's loaded on each printer and optimizes accordingly
- **Spoolman Integration**: Pull filament inventory from your existing Spoolman instance
- **Priority Queue**: Set job priorities and let the scheduler handle the rest
- **Match Scoring**: See how well each job matches the current printer state

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      Web Frontend                           │
│              (React + Timeline Component)                   │
└─────────────────────┬───────────────────────────────────────┘
                      │ REST API
┌─────────────────────▼───────────────────────────────────────┐
│                    FastAPI Backend                          │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐  │
│  │ Job Queue   │  │ Scheduler   │  │ Printer Manager     │  │
│  │ Management  │  │ Engine      │  │ (Filament State)    │  │
│  └─────────────┘  └─────────────┘  └─────────────────────┘  │
└─────────────────────┬───────────────────────────────────────┘
                      │
        ┌─────────────┼─────────────┐
        │             │             │
        ▼             ▼             ▼
   ┌─────────┐  ┌──────────┐  ┌──────────────┐
   │ SQLite  │  │ Spoolman │  │ Printer APIs │
   │   DB    │  │   API    │  │  (Future)    │
   └─────────┘  └──────────┘  └──────────────┘
```

## Quick Start

### Using Docker (Recommended)

```bash
docker-compose up -d
```

The app will be available at `http://localhost:8080`

### Manual Setup

**Backend:**
```bash
cd backend
python -m venv venv
source venv/bin/activate  # or `venv\Scripts\activate` on Windows
pip install -r requirements.txt
uvicorn main:app --reload
```

**Frontend:**
```bash
cd frontend
npm install
npm run dev
```

## Configuration

Environment variables (set in `.env` or `docker-compose.yml`):

| Variable | Description | Default |
|----------|-------------|---------|
| `DATABASE_URL` | SQLite database path | `sqlite:///./printfarm.db` |
| `SPOOLMAN_URL` | Spoolman instance URL (optional) | `None` |
| `BLACKOUT_START` | Start of no-print window (HH:MM) | `22:30` |
| `BLACKOUT_END` | End of no-print window (HH:MM) | `05:30` |

## Concepts

### Job States

- **Pending**: Job is in queue but not scheduled
- **Scheduled**: Job has been assigned a printer and time slot
- **Printing**: Job is actively printing
- **Completed**: Job finished successfully
- **Failed**: Job failed (will not be rescheduled automatically)

### Color Matching

The scheduler calculates a "match score" for each printer-job combination:
- +25 points for each color already loaded
- -10 points for each color that needs to be loaded
- -5 points for each extra color loaded that isn't needed

Jobs are assigned to maximize match scores, minimizing filament changes across your farm.

### Filament Slots

Each printer has N filament slots (typically 4 for AMS-equipped Bambu printers). The scheduler tracks:
- Slot position (1-4)
- Filament type (PLA, PETG, etc.)
- Color name
- Spool ID (if integrated with Spoolman)

## API Documentation

Once running, visit `/docs` for interactive API documentation (Swagger UI).

## Future Roadmap

- [ ] Bambu Lab printer integration (via bambu-connect)
- [ ] OctoPrint integration
- [ ] Klipper/Moonraker integration  
- [ ] Email/webhook notifications
- [ ] Job templates and recurring jobs
- [ ] Cost tracking and reporting
- [ ] Multi-user support

## License

MIT
