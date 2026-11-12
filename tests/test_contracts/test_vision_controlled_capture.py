"""Vigil never writes a training frame for controlled work."""
import numpy as np
import pytest
from sqlalchemy import text
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401


@pytest.fixture
def capture(review_db, tmp_path, monkeypatch):
    from modules.vision import detection_thread, frame_storage
    from modules.vision.detection_thread import PrinterVisionThread
    from modules.vision.inference_engine import VisionInferenceEngine
    monkeypatch.setattr(frame_storage, 'VISION_FRAMES_DIR', str(tmp_path / 'frames'))
    monkeypatch.setattr(detection_thread, 'engine', review_db.get_bind())

    def run(print_job_id):
        thread = PrinterVisionThread(9, 'Owned', VisionInferenceEngine(), {'collect_training_data': 1}, 0, print_job_id, 'IDLE')
        monkeypatch.setattr(thread, '_capture_frame', lambda: np.full((32, 32, 3), 100, dtype=np.uint8))
        thread._capture_and_analyze()
        return len(list((tmp_path / 'frames').rglob('*.jpg')))
    return run


def _print(db, print_id, job_id):
    db.execute(text("INSERT INTO jobs(id,item_name,status,printer_id) VALUES(:j,'part','printing',9)"), {'j': job_id})
    db.execute(text("INSERT INTO print_jobs(id,printer_id,job_name,status,started_at,scheduled_job_id) "
                    "VALUES(:p,9,'part','running','2026-11-12 00:00:00',:j)"), {'p': print_id, 'j': job_id})
    db.commit()


def _mark(db, job_id):
    db.execute(text("INSERT OR IGNORE INTO controlled_audiences(id,name) VALUES(1,'Secure')"))
    db.execute(text("INSERT INTO controlled_jobs(job_id,audience_id,label,printer_tag,retention_days,marked_at,reason) "
                    "VALUES(:j,1,'CUI','Secure',7,'2026-01-01T00:00:00+00:00','test')"), {'j': job_id})
    db.commit()


def test_uncontrolled_print_is_collected(capture, review_db):
    _print(review_db, 9001, 8001)
    assert capture(9001) == 1


def test_controlled_print_is_not_collected_and_leaves_no_row(capture, review_db):
    _print(review_db, 9001, 8001)
    _mark(review_db, 8001)
    assert capture(9001) == 0
    assert review_db.execute(text("SELECT COUNT(*) FROM vision_detections WHERE status='training'")).scalar() == 0


def test_another_jobs_mark_does_not_stop_collection_for_open_work(capture, review_db):
    _print(review_db, 9001, 8001)
    _print(review_db, 9002, 8002)
    _mark(review_db, 8002)
    assert capture(9001) == 1


def test_a_print_with_no_job_is_not_collected_once_controlled_work_exists(capture, review_db):
    assert capture(None) == 1                      # no controlled data anywhere: collected as before
    _print(review_db, 9002, 8002)
    _mark(review_db, 8002)
    assert capture(None) == 1                      # nothing new written


def test_a_failed_check_saves_nothing(capture, review_db, monkeypatch):
    from core import controlled_parts
    _print(review_db, 9001, 8001)

    def broken(db):
        raise RuntimeError('database unavailable')
    monkeypatch.setattr(controlled_parts, 'any_controlled_data', broken)
    assert capture(9001) == 0


@pytest.mark.parametrize('provenance', ['unlinked', 'missing_print', 'missing_job', 'wrong_printer'])
def test_unknown_print_provenance_is_refused(capture, review_db, provenance):
    _print(review_db, 9002, 8002)
    _mark(review_db, 8002)
    if provenance != 'missing_print':
        _print(review_db, 9001, 8001)
        if provenance == 'unlinked':
            review_db.execute(text('UPDATE print_jobs SET scheduled_job_id=NULL WHERE id=9001'))
        elif provenance == 'missing_job':
            review_db.execute(text('DELETE FROM jobs WHERE id=8001'))
        else:
            review_db.execute(text('UPDATE print_jobs SET printer_id=10 WHERE id=9001'))
        review_db.commit()
    assert capture(9001) == 0
    assert review_db.execute(text("SELECT COUNT(*) FROM vision_detections WHERE status='training'")).scalar() == 0
