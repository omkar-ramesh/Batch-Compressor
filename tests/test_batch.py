import time

from PIL import Image

from compressor.batch import BatchRunner
from compressor.engine import STATUS_FAILED, STATUS_COPIED
from compressor.settings import Settings
from test_engine import noisy_image


def wait_for(job, timeout=60):
    end = time.time() + timeout
    while job.state == "running" and time.time() < end:
        time.sleep(0.05)
    assert job.state != "running", "job did not finish in time"


def make_files(tmp_path, n, size=(200, 200)):
    files = []
    for i in range(n):
        p = tmp_path / f"img{i}.jpg"
        Image.new("RGB", size, (i * 10 % 255, 50, 90)).save(p, "JPEG", quality=90)
        files.append(str(p))
    return files


def test_job_runs_to_completion(tmp_path):
    files = make_files(tmp_path, 4)
    job = BatchRunner().start(files, Settings(max_mb=8, workers=2))
    wait_for(job)
    snap = job.snapshot()
    assert snap["state"] == "finished"
    assert snap["total"] == snap["done"] == 4
    assert snap["counts"][STATUS_COPIED] == 4
    assert snap["original_bytes"] == snap["new_bytes"] > 0


def test_missing_and_corrupt_files_do_not_stop_batch(tmp_path):
    good = make_files(tmp_path, 2)
    bad = tmp_path / "junk.jpg"
    bad.write_bytes(b"not an image" * 50)
    files = [str(tmp_path / "ghost.jpg"), str(bad)] + good
    job = BatchRunner().start(files, Settings(max_mb=8, workers=2))
    wait_for(job)
    snap = job.snapshot()
    assert snap["state"] == "finished"
    assert snap["done"] == 4
    assert snap["counts"][STATUS_FAILED] == 2
    assert snap["counts"][STATUS_COPIED] == 2


def test_cancel_stops_dispatch(tmp_path):
    # One worker + heavy files => most are still queued when we cancel.
    heavy = tmp_path / "heavy.jpg"
    noisy_image(2400, 1600).save(heavy, "JPEG", quality=100)
    files = [str(heavy)] * 12
    job = BatchRunner().start(files, Settings(max_mb=0.5, workers=1, on_conflict="overwrite"))
    time.sleep(0.5)
    job.cancel()
    wait_for(job, timeout=120)
    snap = job.snapshot()
    assert snap["state"] == "cancelled"
    assert snap["done"] < snap["total"]


def test_runner_tracks_jobs_by_id(tmp_path):
    runner = BatchRunner()
    job = runner.start(make_files(tmp_path, 1), Settings(workers=1))
    assert runner.get(job.id) is job
    assert runner.get("nope") is None
    wait_for(job)
