"""
Test Moonraker Adapter

Quick test to verify connectivity to the Kobra S1 via Rinkhals/Moonraker.

Usage:
    cd /opt/printfarm-scheduler/backend
    source venv/bin/activate
    python3 test_moonraker.py
    
    # Or with custom host:
    python3 test_moonraker.py 192.168.72.133 80
"""

import sys
import json

sys.path.insert(0, "/opt/printfarm-scheduler/backend")

from moonraker_adapter import MoonrakerPrinter


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "192.168.72.133"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 80

    print(f"Connecting to Moonraker at {host}:{port}...")
    printer = MoonrakerPrinter(host=host, port=port)

    if not printer.connect():
        print("FAILED to connect")
        sys.exit(1)

    print(f"Connected to: {printer._device_type}")
    print()

    # Get full status
    status = printer.get_status()

    print("=== Printer Status ===")
    print(f"  State:        {status.state.value} (internal: {status.internal_state})")
    print(f"  Device:       {status.device_type}")
    print(f"  Bed temp:     {status.bed_temp}°C (target: {status.bed_target}°C)")
    print(f"  Nozzle temp:  {status.nozzle_temp}°C (target: {status.nozzle_target}°C)")
    print()

    if status.filename:
        print("=== Print Progress ===")
        print(f"  File:         {status.filename}")
        print(f"  Progress:     {status.progress_percent}%")
        print(f"  Layer:        {status.current_layer}/{status.total_layers}")
        print(f"  Duration:     {status.print_duration:.0f}s")
        print(f"  Filament:     {status.filament_used_mm:.1f}mm")
        print()

    if status.filament_slots:
        print("=== ACE Filament Slots ===")
        for slot in status.filament_slots:
            loaded = "✓" if slot.loaded else "✗"
            color = slot.color_hex[:6] if slot.color_hex else "------"
            print(f"  Gate {slot.gate}: [{loaded}] {slot.material:6s} #{color} {slot.name}")
        print()

    # Webcam
    cams = printer.get_webcam_urls()
    if cams.get("stream_url"):
        print("=== Webcam ===")
        print(f"  Stream:   {cams['stream_url']}")
        print(f"  Snapshot: {cams['snapshot_url']}")
        print()

    # Job history
    history = printer.get_job_history(limit=5)
    if history:
        print(f"=== Recent Jobs ({len(history)}) ===")
        for job in history:
            fname = job.get("filename", "?")
            jstatus = job.get("status", "?")
            duration = job.get("total_duration", 0)
            mins = int(duration / 60)
            print(f"  {fname:40s} {jstatus:10s} {mins}min")
        print()

    printer.disconnect()
    print("Done.")


if __name__ == "__main__":
    main()
