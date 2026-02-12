"""
Timelapse Capture Daemon

Periodically captures JPEG frames from go2rtc camera streams for printers
with timelapse_enabled=True while they are actively printing. When a print
completes, stitches frames into an MP4 video using ffmpeg.
"""

import os
import time
import logging
import subprocess
import shutil
from datetime import datetime
from pathlib import Path

import httpx
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [timelapse] %(levelname)s %(message)s",
)
log = logging.getLogger("odin.timelapse")

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:////data/odin.db")
GO2RTC_BASE = "http://127.0.0.1:1984"
TIMELAPSE_DIR = Path("/data/timelapses")
CAPTURE_INTERVAL = 30  # seconds between frames
FFMPEG_FPS = 30  # output video framerate

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine)


def get_active_printers(session) -> list:
    """Get printers that have timelapse enabled and are currently printing."""
    rows = session.execute(text("""
        SELECT p.id, p.name
        FROM printers p
        WHERE p.timelapse_enabled = 1
          AND p.is_active = 1
          AND p.gcode_state = 'RUNNING'
    """)).fetchall()
    return [{"id": r[0], "name": r[1]} for r in rows]


def get_active_job_for_printer(session, printer_id: int) -> dict | None:
    """Get the currently-running job for a printer."""
    row = session.execute(text("""
        SELECT id, item_name FROM jobs
        WHERE printer_id = :pid AND status = 'printing'
        ORDER BY actual_start DESC LIMIT 1
    """), {"pid": printer_id}).fetchone()
    if row:
        return {"id": row[0], "item_name": row[1]}
    return None


def get_or_create_timelapse(session, printer_id: int, job_id: int) -> int:
    """Get existing capturing timelapse or create a new one."""
    row = session.execute(text("""
        SELECT id FROM timelapses
        WHERE printer_id = :pid AND print_job_id = :jid AND status = 'capturing'
        LIMIT 1
    """), {"pid": printer_id, "jid": job_id}).fetchone()
    if row:
        return row[0]

    # Create new timelapse
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"printer_{printer_id}/{ts}.mp4"
    session.execute(text("""
        INSERT INTO timelapses (printer_id, print_job_id, filename, frame_count, status, created_at)
        VALUES (:pid, :jid, :fname, 0, 'capturing', datetime('now'))
    """), {"pid": printer_id, "jid": job_id, "fname": filename})
    session.commit()

    row = session.execute(text("""
        SELECT id FROM timelapses
        WHERE printer_id = :pid AND print_job_id = :jid AND status = 'capturing'
        ORDER BY id DESC LIMIT 1
    """), {"pid": printer_id, "jid": job_id}).fetchone()
    return row[0]


def capture_frame(printer_id: int, timelapse_id: int) -> bool:
    """Capture a single JPEG frame from go2rtc."""
    url = f"{GO2RTC_BASE}/api/frame.jpeg?src=printer_{printer_id}"
    try:
        resp = httpx.get(url, timeout=5)
        if resp.status_code != 200:
            return False
        if len(resp.content) < 1000:  # too small, probably error
            return False

        frame_dir = TIMELAPSE_DIR / str(timelapse_id) / "frames"
        frame_dir.mkdir(parents=True, exist_ok=True)

        # Sequential numbering for ffmpeg
        existing = list(frame_dir.glob("*.jpg"))
        frame_num = len(existing) + 1
        frame_path = frame_dir / f"frame_{frame_num:06d}.jpg"
        frame_path.write_bytes(resp.content)
        return True
    except Exception as e:
        log.debug(f"Frame capture failed for printer {printer_id}: {e}")
        return False


def encode_timelapse(timelapse_id: int) -> bool:
    """Stitch captured frames into MP4 using ffmpeg."""
    session = SessionLocal()
    try:
        row = session.execute(text("""
            SELECT id, filename, printer_id FROM timelapses WHERE id = :tid
        """), {"tid": timelapse_id}).fetchone()
        if not row:
            return False

        frame_dir = TIMELAPSE_DIR / str(timelapse_id) / "frames"
        frames = sorted(frame_dir.glob("*.jpg"))
        if len(frames) < 2:
            # Not enough frames, mark as failed
            session.execute(text("""
                UPDATE timelapses SET status = 'failed', completed_at = datetime('now')
                WHERE id = :tid
            """), {"tid": timelapse_id})
            session.commit()
            log.warning(f"Timelapse {timelapse_id}: only {len(frames)} frames, skipping encode")
            return False

        output_dir = TIMELAPSE_DIR / f"printer_{row[2]}"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = TIMELAPSE_DIR / row[1]

        session.execute(text("""
            UPDATE timelapses SET status = 'encoding' WHERE id = :tid
        """), {"tid": timelapse_id})
        session.commit()

        # ffmpeg: input pattern -> mp4
        cmd = [
            "ffmpeg", "-y",
            "-framerate", str(FFMPEG_FPS),
            "-i", str(frame_dir / "frame_%06d.jpg"),
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(output_path),
        ]

        result = subprocess.run(cmd, capture_output=True, timeout=300)
        if result.returncode != 0:
            log.error(f"ffmpeg failed for timelapse {timelapse_id}: {result.stderr.decode()[-500:]}")
            session.execute(text("""
                UPDATE timelapses SET status = 'failed', completed_at = datetime('now')
                WHERE id = :tid
            """), {"tid": timelapse_id})
            session.commit()
            return False

        # Update record
        file_size = output_path.stat().st_size / (1024 * 1024)
        duration = len(frames) / FFMPEG_FPS
        session.execute(text("""
            UPDATE timelapses
            SET status = 'ready',
                frame_count = :fc,
                duration_seconds = :dur,
                file_size_mb = :sz,
                completed_at = datetime('now')
            WHERE id = :tid
        """), {"tid": timelapse_id, "fc": len(frames), "dur": round(duration, 1), "sz": round(file_size, 2)})
        session.commit()

        # Clean up frames
        shutil.rmtree(TIMELAPSE_DIR / str(timelapse_id), ignore_errors=True)
        log.info(f"Timelapse {timelapse_id}: encoded {len(frames)} frames -> {file_size:.1f} MB")
        return True

    except Exception as e:
        log.error(f"Encode error for timelapse {timelapse_id}: {e}")
        session.execute(text("""
            UPDATE timelapses SET status = 'failed', completed_at = datetime('now')
            WHERE id = :tid
        """), {"tid": timelapse_id})
        session.commit()
        return False
    finally:
        session.close()


def finalize_stale_timelapses():
    """Find timelapses still in 'capturing' state whose print has ended and encode them."""
    session = SessionLocal()
    try:
        rows = session.execute(text("""
            SELECT t.id, t.printer_id, t.print_job_id
            FROM timelapses t
            WHERE t.status = 'capturing'
        """)).fetchall()

        for row in rows:
            tid, pid, jid = row[0], row[1], row[2]
            # Check if the print is still running
            job = session.execute(text("""
                SELECT status FROM jobs WHERE id = :jid
            """), {"jid": jid}).fetchone()

            if not job or job[0] != 'printing':
                log.info(f"Timelapse {tid}: print job {jid} no longer printing, encoding...")
                encode_timelapse(tid)

    except Exception as e:
        log.error(f"Error finalizing stale timelapses: {e}")
    finally:
        session.close()


def main_loop():
    """Main capture loop."""
    TIMELAPSE_DIR.mkdir(parents=True, exist_ok=True)
    log.info("Timelapse capture daemon started")

    # Track active captures: {(printer_id, job_id): timelapse_id}
    active = {}
    tick = 0

    while True:
        try:
            session = SessionLocal()
            printers = get_active_printers(session)

            current_keys = set()
            for printer in printers:
                job = get_active_job_for_printer(session, printer["id"])
                if not job:
                    continue

                key = (printer["id"], job["id"])
                current_keys.add(key)

                tid = active.get(key)
                if not tid:
                    tid = get_or_create_timelapse(session, printer["id"], job["id"])
                    active[key] = tid
                    log.info(f"Capturing timelapse {tid} for {printer['name']} (job {job['id']})")

                if capture_frame(printer["id"], tid):
                    session.execute(text("""
                        UPDATE timelapses SET frame_count = frame_count + 1 WHERE id = :tid
                    """), {"tid": tid})
                    session.commit()

            # Check for prints that just ended
            ended = set(active.keys()) - current_keys
            for key in ended:
                tid = active.pop(key)
                log.info(f"Print ended for timelapse {tid}, starting encode...")
                encode_timelapse(tid)

            session.close()

            # Every 5 minutes, check for stale timelapses (e.g. daemon was restarted)
            if tick % 10 == 0:
                finalize_stale_timelapses()

            tick += 1

        except Exception as e:
            log.error(f"Main loop error: {e}")

        time.sleep(CAPTURE_INTERVAL)


if __name__ == "__main__":
    main_loop()
