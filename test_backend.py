#!/usr/bin/env python3
"""
Quick test to verify the PrintFarm Scheduler backend runs correctly.

Run this after setup to check for import errors and basic functionality.
"""

import sys
from pathlib import Path

# Add backend to path
backend_path = Path(__file__).parent / "backend"
sys.path.insert(0, str(backend_path))

def test_imports():
    """Test that all modules import correctly."""
    print("Testing imports...")
    
    try:
        from models import Printer, Job, Model, FilamentSlot, JobStatus, FilamentType
        print("  ✓ models.py")
    except Exception as e:
        print(f"  ✗ models.py: {e}")
        return False
    
    try:
        from schemas import (
            PrinterCreate, PrinterResponse,
            JobCreate, JobResponse, 
            ModelCreate, ModelResponse,
        )
        print("  ✓ schemas.py")
    except Exception as e:
        print(f"  ✗ schemas.py: {e}")
        return False
    
    try:
        from scheduler import Scheduler, SchedulerConfig, run_scheduler
        print("  ✓ scheduler.py")
    except Exception as e:
        print(f"  ✗ scheduler.py: {e}")
        return False
    
    try:
        from config import settings
        print("  ✓ config.py")
    except Exception as e:
        print(f"  ✗ config.py: {e}")
        return False
    
    try:
        from main import app
        print("  ✓ main.py")
    except Exception as e:
        print(f"  ✗ main.py: {e}")
        return False
    
    return True


def test_database():
    """Test database creation and basic operations."""
    print("\nTesting database...")
    
    import tempfile
    import os
    
    # Use a temp database
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    
    try:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from models import Base, Printer, FilamentSlot, Job, Model, JobStatus, FilamentType
        
        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)
        print("  ✓ Database tables created")
        
        Session = sessionmaker(bind=engine)
        session = Session()
        
        # Create a test printer
        printer = Printer(name="Test-P1S", model="Bambu Lab P1S", slot_count=4, is_active=True)
        session.add(printer)
        session.flush()
        
        # Add filament slots
        for i in range(4):
            slot = FilamentSlot(
                printer_id=printer.id,
                slot_number=i + 1,
                filament_type=FilamentType.PLA,
                color=["black", "white", "red", "blue"][i]
            )
            session.add(slot)
        
        session.commit()
        print("  ✓ Created test printer with filament slots")
        
        # Create a test job
        job = Job(
            item_name="Test Print",
            status=JobStatus.PENDING,
            priority=3,
            duration_hours=2.5,
            colors_required="black, white"
        )
        session.add(job)
        session.commit()
        print("  ✓ Created test job")
        
        # Query back
        printers = session.query(Printer).all()
        jobs = session.query(Job).all()
        print(f"  ✓ Query OK: {len(printers)} printers, {len(jobs)} jobs")
        
        # Test printer loaded_colors property
        p = printers[0]
        colors = p.loaded_colors
        print(f"  ✓ Printer loaded colors: {colors}")
        
        session.close()
        return True
        
    except Exception as e:
        print(f"  ✗ Database test failed: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        os.unlink(db_path)


def test_scheduler():
    """Test the scheduler logic."""
    print("\nTesting scheduler...")
    
    import tempfile
    import os
    
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    
    try:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from models import Base, Printer, FilamentSlot, Job, JobStatus, FilamentType
        from scheduler import Scheduler, SchedulerConfig
        
        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        session = Session()
        
        # Create printers
        for name, colors in [("P1S", ["black", "white", "red", "green"]), 
                              ("X1C", ["black", "white", "blue", "yellow"])]:
            printer = Printer(name=name, slot_count=4, is_active=True)
            session.add(printer)
            session.flush()
            
            for i, color in enumerate(colors):
                slot = FilamentSlot(
                    printer_id=printer.id,
                    slot_number=i + 1,
                    color=color
                )
                session.add(slot)
        
        # Create jobs with different color requirements
        jobs_data = [
            ("Job A", "black, white", 2),       # Should match both printers well
            ("Job B", "black, red", 3),         # Should prefer P1S
            ("Job C", "blue, yellow", 4),       # Should prefer X1C
            ("Job D", "black, white, red", 5),  # Should prefer P1S
        ]
        
        for name, colors, duration in jobs_data:
            job = Job(
                item_name=name,
                status=JobStatus.PENDING,
                priority=3,
                duration_hours=duration,
                colors_required=colors
            )
            session.add(job)
        
        session.commit()
        print("  ✓ Created test printers and jobs")
        
        # Run scheduler
        scheduler = Scheduler(SchedulerConfig())
        result = scheduler.run(session)
        
        print(f"  ✓ Scheduler ran: {result.scheduled_count} scheduled, {result.skipped_count} skipped")
        print(f"  ✓ Setup blocks needed: {result.setup_blocks}")
        print(f"  ✓ Avg match score: {result.avg_match_score:.1f}")
        
        # Check assignments
        for job in session.query(Job).filter(Job.status == JobStatus.SCHEDULED).all():
            print(f"    - {job.item_name} -> Printer {job.printer_id} (score: {job.match_score})")
        
        session.close()
        return True
        
    except Exception as e:
        print(f"  ✗ Scheduler test failed: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        os.unlink(db_path)


def test_api():
    """Test that the FastAPI app starts correctly."""
    print("\nTesting API startup...")
    
    try:
        from fastapi.testclient import TestClient
        from main import app
        
        client = TestClient(app)
        
        # Test health endpoint
        response = client.get("/health")
        if response.status_code == 200:
            print(f"  ✓ Health check: {response.json()}")
        else:
            print(f"  ✗ Health check failed: {response.status_code}")
            return False
        
        # Test stats endpoint
        response = client.get("/api/stats")
        if response.status_code == 200:
            print(f"  ✓ Stats endpoint: {response.json()}")
        else:
            print(f"  ✗ Stats endpoint failed: {response.status_code}")
            return False
        
        # Test printers endpoint
        response = client.get("/api/printers")
        if response.status_code == 200:
            print(f"  ✓ Printers endpoint: {len(response.json())} printers")
        else:
            print(f"  ✗ Printers endpoint failed: {response.status_code}")
            return False
        
        return True
        
    except Exception as e:
        print(f"  ✗ API test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    print("🖨️  PrintFarm Scheduler - Backend Tests")
    print("=" * 45)
    
    all_passed = True
    
    all_passed &= test_imports()
    all_passed &= test_database()
    all_passed &= test_scheduler()
    all_passed &= test_api()
    
    print("\n" + "=" * 45)
    if all_passed:
        print("✅ All tests passed! Backend is ready.")
        print("\nTo start the server:")
        print("  cd backend")
        print("  uvicorn main:app --reload")
    else:
        print("❌ Some tests failed. Check the errors above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
