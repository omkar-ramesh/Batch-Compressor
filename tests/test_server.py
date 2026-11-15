import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import server


@pytest.fixture
def client():
    return TestClient(server.app)


@pytest.fixture
def images(tmp_path):
    paths = []
    for i in range(3):
        p = tmp_path / f"p{i}.jpg"
        Image.new("RGB", (300, 200), (i * 60, 100, 150)).save(p, "JPEG", quality=90)
        paths.append(str(p))
    return paths


def test_defaults(client):
    r = client.get("/api/defaults").json()
    assert "settings" in r and r["cpu_count"] >= 1


def test_scan_lists_images(client, tmp_path, images):
    (tmp_path / "notes.txt").write_text("x")
    r = client.post("/api/scan", json={"paths": [str(tmp_path)]}).json()
    assert len(r["files"]) == 3
    assert r["total_bytes"] == sum(f["bytes"] for f in r["files"])


def test_job_lifecycle(client, images):
    jid = client.post("/api/jobs", json={"files": images, "settings": {"max_mb": 8, "workers": 2}}).json()["job_id"]
    deadline = time.time() + 60
    while time.time() < deadline:
        snap = client.get(f"/api/jobs/{jid}").json()
        if snap["state"] != "running":
            break
        time.sleep(0.1)
    assert snap["state"] == "finished" and snap["done"] == 3

    # `since` returns only unseen results
    tail = client.get(f"/api/jobs/{jid}", params={"since": 2}).json()
    assert len(tail["results"]) == 1


def test_job_errors(client):
    assert client.post("/api/jobs", json={"files": []}).status_code == 400
    assert client.get("/api/jobs/missing").status_code == 404
    assert client.post("/api/jobs/missing/cancel").status_code == 404


def test_thumb(client, images):
    r = client.get("/api/thumb", params={"path": images[0], "size": 100})
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert client.get("/api/thumb", params={"path": "Z:/nope.jpg"}).status_code == 404


def test_raw_file(client, images, tmp_path):
    r = client.get("/api/file", params={"path": images[0]})
    assert r.status_code == 200 and r.content == open(images[0], "rb").read()
    (tmp_path / "x.txt").write_text("secret")
    assert client.get("/api/file", params={"path": str(tmp_path / "x.txt")}).status_code == 415


def test_preview_endpoint(client, images):
    import json
    r = client.post("/api/preview", json={"path": images[0], "settings": {"max_mb": 0.01, "measure_quality": True}})
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    res = json.loads(r.headers["x-result"])
    assert res["new_bytes"] == len(r.content) and res["destination"] is None


def test_open_missing_path(client):
    assert client.post("/api/open", json={"path": "Z:/nope"}).status_code == 404


def test_reset_keeps_running_jobs_out_of_the_purge(client, images):
    jid = client.post("/api/jobs", json={"files": images, "settings": {"workers": 1}}).json()["job_id"]
    while client.get(f"/api/jobs/{jid}").json()["state"] == "running":
        time.sleep(0.1)
    out = client.post("/api/reset").json()
    assert out["ok"] and out["jobs_cleared"] >= 1
    assert client.get(f"/api/jobs/{jid}").status_code == 404
