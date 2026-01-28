#!/usr/bin/env python3
"""
PrintFarm Scheduler - Google Sheets Import Tool

This script imports data from your existing Google Sheets setup into the 
PrintFarm Scheduler database.

Usage:
    1. Export your Google Sheets as CSV files:
       - Jobs sheet -> jobs.csv
       - Pricing sheet -> models.csv (for the model definitions)
       - PrinterConfig sheet -> printers.csv
    
    2. Run this script:
       python import_from_sheets.py --jobs jobs.csv --models models.csv --printers printers.csv

    Or import individually:
       python import_from_sheets.py --printers printers.csv
       python import_from_sheets.py --models models.csv
       python import_from_sheets.py --jobs jobs.csv
"""

import argparse
import csv
import sys
from pathlib import Path
from datetime import datetime
from typing import Optional

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent / "backend"))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from models import Base, Printer, FilamentSlot, Model, Job, JobStatus, FilamentType


def parse_colors(color_string: str) -> list[str]:
    """Parse comma-separated color string into list."""
    if not color_string:
        return []
    return [c.strip().lower() for c in color_string.split(",") if c.strip()]


def parse_filament_type(type_str: str) -> FilamentType:
    """Convert string to FilamentType enum."""
    type_str = (type_str or "PLA").upper().strip()
    type_map = {
        "PLA": FilamentType.PLA,
        "PETG": FilamentType.PETG,
        "ABS": FilamentType.ABS,
        "ASA": FilamentType.ASA,
        "TPU": FilamentType.TPU,
        "PA": FilamentType.PA,
        "NYLON": FilamentType.PA,
        "PC": FilamentType.PC,
        "PVA": FilamentType.PVA,
    }
    return type_map.get(type_str, FilamentType.OTHER)


def parse_job_status(status_str: str) -> JobStatus:
    """Convert string to JobStatus enum."""
    status_str = (status_str or "pending").lower().strip()
    status_map = {
        "pending": JobStatus.PENDING,
        "scheduled": JobStatus.SCHEDULED,
        "printing": JobStatus.PRINTING,
        "completed": JobStatus.COMPLETED,
        "complete": JobStatus.COMPLETED,
        "failed": JobStatus.FAILED,
        "cancelled": JobStatus.CANCELLED,
        "canceled": JobStatus.CANCELLED,
    }
    return status_map.get(status_str, JobStatus.PENDING)


def parse_datetime(dt_str: str) -> Optional[datetime]:
    """Try to parse various datetime formats."""
    if not dt_str or dt_str.strip() == "":
        return None
    
    formats = [
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y %I:%M:%S %p",
        "%m/%d/%y %H:%M:%S",
        "%m/%d/%y %I:%M %p",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%m/%d %H:%M:%S",
        "%m/%d %I:%M %p",
    ]
    
    for fmt in formats:
        try:
            return datetime.strptime(dt_str.strip(), fmt)
        except ValueError:
            continue
    
    print(f"  ⚠ Could not parse datetime: {dt_str}")
    return None


def parse_float(val: str) -> Optional[float]:
    """Safely parse float."""
    if not val or val.strip() == "":
        return None
    try:
        # Remove $ and other common formatting
        cleaned = val.replace("$", "").replace(",", "").strip()
        return float(cleaned)
    except ValueError:
        return None


def parse_int(val: str) -> Optional[int]:
    """Safely parse int."""
    if not val or val.strip() == "":
        return None
    try:
        return int(float(val))
    except ValueError:
        return None


def import_printers(session, csv_path: Path):
    """
    Import printers from PrinterConfig CSV.
    
    Expected columns:
    - Printer (name)
    - Loaded Colors (comma-separated)
    """
    print(f"\n📥 Importing printers from {csv_path}")
    
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        
        # Normalize column names
        reader.fieldnames = [n.strip().lower().replace(" ", "_") for n in reader.fieldnames]
        
        count = 0
        for row in reader:
            name = row.get("printer", "").strip()
            if not name:
                continue
            
            # Check if printer exists
            existing = session.query(Printer).filter(Printer.name == name).first()
            if existing:
                print(f"  ⏭ Printer '{name}' already exists, skipping")
                continue
            
            # Parse loaded colors
            colors_str = row.get("loaded_colors", "") or row.get("colors", "")
            colors = parse_colors(colors_str)
            
            # Create printer
            printer = Printer(
                name=name,
                model="Bambu Lab",  # Default, can be updated later
                slot_count=max(4, len(colors)),
                is_active=True,
            )
            session.add(printer)
            session.flush()  # Get the ID
            
            # Create filament slots
            for i in range(printer.slot_count):
                color = colors[i] if i < len(colors) else None
                slot = FilamentSlot(
                    printer_id=printer.id,
                    slot_number=i + 1,
                    filament_type=FilamentType.PLA,
                    color=color,
                )
                session.add(slot)
            
            count += 1
            print(f"  ✓ Added printer: {name} with {len(colors)} colors")
        
        session.commit()
        print(f"  📊 Imported {count} printers")


def import_models(session, csv_path: Path):
    """
    Import models from Pricing CSV.
    
    Expected columns (based on your screenshot):
    - Model Name / Name
    - Build Time (hrs)
    - Total Cost ($)
    - Cost Per Item ($)
    - Color 1 Type, Color 1 Used (g)
    - Color 2 Type, Color 2 Used (g)
    - etc.
    """
    print(f"\n📥 Importing models from {csv_path}")
    
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        
        # Normalize column names
        reader.fieldnames = [n.strip().lower().replace(" ", "_") for n in reader.fieldnames]
        
        count = 0
        for row in reader:
            name = row.get("model_name", "") or row.get("name", "")
            name = name.strip()
            if not name:
                continue
            
            # Check if model exists
            existing = session.query(Model).filter(Model.name == name).first()
            if existing:
                print(f"  ⏭ Model '{name}' already exists, skipping")
                continue
            
            # Parse build time
            build_time = parse_float(row.get("build_time_(hrs)", "") or row.get("build_time", ""))
            
            # Parse cost
            cost = parse_float(row.get("cost_per_item_($)", "") or row.get("cost_per_item", "") or row.get("total_cost_($)", ""))
            
            # Parse color requirements
            color_requirements = {}
            for i in range(1, 5):  # Up to 4 colors
                color_type_key = f"color_{i}_type"
                color_grams_key = f"color_{i}_used_(g)" 
                
                # Try alternate column names
                if color_type_key not in row:
                    color_type_key = f"color{i}_type"
                if color_grams_key not in row:
                    color_grams_key = f"color{i}_used"
                    if color_grams_key not in row:
                        color_grams_key = f"color_{i}_grams"
                
                color_name = row.get(color_type_key, "").strip()
                color_grams = parse_float(row.get(color_grams_key, ""))
                
                if color_name:
                    color_requirements[f"color{i}"] = {
                        "color": color_name.lower(),
                        "grams": color_grams or 0,
                    }
            
            # Create model
            model = Model(
                name=name,
                build_time_hours=build_time,
                cost_per_item=cost,
                color_requirements=color_requirements if color_requirements else None,
                default_filament_type=FilamentType.PLA,
            )
            session.add(model)
            count += 1
            print(f"  ✓ Added model: {name}")
        
        session.commit()
        print(f"  📊 Imported {count} models")


def import_jobs(session, csv_path: Path):
    """
    Import jobs from Jobs CSV.
    
    Expected columns (based on your screenshot):
    - Item
    - Printer
    - Status
    - Priority
    - Colors Used
    - Start Time
    - Duration (hrs)
    - End Time
    - Match Score (%)
    - Lock
    """
    print(f"\n📥 Importing jobs from {csv_path}")
    
    # Build printer name -> id map
    printers = {p.name: p.id for p in session.query(Printer).all()}
    
    # Build model name -> id map
    models = {m.name.lower(): m.id for m in session.query(Model).all()}
    
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        
        # Normalize column names
        reader.fieldnames = [n.strip().lower().replace(" ", "_") for n in reader.fieldnames]
        
        count = 0
        skipped = 0
        for row in reader:
            item_name = row.get("item", "").strip()
            if not item_name:
                continue
            
            # Parse status
            status = parse_job_status(row.get("status", "pending"))
            
            # Skip completed/failed jobs older than 7 days (optional)
            # if status in [JobStatus.COMPLETED, JobStatus.FAILED]:
            #     skipped += 1
            #     continue
            
            # Parse printer
            printer_name = row.get("printer", "").strip()
            printer_id = printers.get(printer_name) if printer_name else None
            
            # Try to match to a model
            model_id = models.get(item_name.lower())
            
            # Parse other fields
            priority = parse_int(row.get("priority", "3")) or 3
            duration = parse_float(row.get("duration_(hrs)", "") or row.get("duration", ""))
            colors = row.get("colors_used", "").strip()
            start_time = parse_datetime(row.get("start_time", ""))
            end_time = parse_datetime(row.get("end_time", ""))
            match_score = parse_int(row.get("match_score_(%)", "") or row.get("match_score", ""))
            is_locked = row.get("lock", "").strip().lower() in ["true", "1", "yes", "locked"]
            
            # Create job
            job = Job(
                item_name=item_name,
                model_id=model_id,
                status=status,
                priority=priority,
                printer_id=printer_id,
                duration_hours=duration,
                colors_required=colors if colors else None,
                scheduled_start=start_time,
                scheduled_end=end_time,
                match_score=match_score,
                is_locked=is_locked or status in [JobStatus.COMPLETED, JobStatus.PRINTING],
            )
            
            # Set actual times for completed jobs
            if status == JobStatus.COMPLETED:
                job.actual_start = start_time
                job.actual_end = end_time
            elif status == JobStatus.PRINTING:
                job.actual_start = start_time
            
            session.add(job)
            count += 1
        
        session.commit()
        print(f"  📊 Imported {count} jobs (skipped {skipped})")


def main():
    parser = argparse.ArgumentParser(
        description="Import data from Google Sheets CSV exports into PrintFarm Scheduler"
    )
    parser.add_argument("--printers", type=Path, help="Path to printers CSV (PrinterConfig export)")
    parser.add_argument("--models", type=Path, help="Path to models CSV (Pricing export)")
    parser.add_argument("--jobs", type=Path, help="Path to jobs CSV (Jobs export)")
    parser.add_argument("--db", type=str, default="sqlite:///./backend/printfarm.db", 
                        help="Database URL (default: sqlite:///./backend/printfarm.db)")
    parser.add_argument("--reset", action="store_true", help="Reset database before import (DELETES ALL DATA)")
    
    args = parser.parse_args()
    
    if not any([args.printers, args.models, args.jobs]):
        parser.print_help()
        print("\n❌ Error: Specify at least one CSV file to import")
        sys.exit(1)
    
    # Connect to database
    print(f"🔌 Connecting to database: {args.db}")
    engine = create_engine(args.db)
    
    if args.reset:
        print("⚠️  Resetting database (dropping all tables)...")
        Base.metadata.drop_all(engine)
    
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    
    try:
        # Import in order: printers -> models -> jobs
        if args.printers:
            if not args.printers.exists():
                print(f"❌ File not found: {args.printers}")
                sys.exit(1)
            import_printers(session, args.printers)
        
        if args.models:
            if not args.models.exists():
                print(f"❌ File not found: {args.models}")
                sys.exit(1)
            import_models(session, args.models)
        
        if args.jobs:
            if not args.jobs.exists():
                print(f"❌ File not found: {args.jobs}")
                sys.exit(1)
            import_jobs(session, args.jobs)
        
        print("\n✅ Import complete!")
        
    except Exception as e:
        session.rollback()
        print(f"\n❌ Error during import: {e}")
        raise
    finally:
        session.close()


if __name__ == "__main__":
    main()
