from __future__ import annotations


def create_well(client, code="W-100"):
    response = client.post("/api/hydro/wells", json={"code": code, "name": "北部监测井", "latitude": 35.1, "longitude": 116.2, "aquifer": "浅层孔隙含水层", "screen_depth_m": 42})
    assert response.status_code == 201, response.text
    return response.json()


def submit(client, key="BATCH-001", rows=None, source="甲实验室"):
    return client.post("/api/hydro/imports", json={"batch_key": key, "source": source, "rows": rows})


def mixed_rows():
    return [
        {"well_code": "w-100", "sampled_at": "2026-09-20T08:00:00+08:00", "observation_type": "solute", "unit": "µg/L", "value": 12500, "detection_limit": 5, "measurement_error": 0.2},
        {"well_code": "W-100", "sampled_at": "2026-09-20T08:00:00+08:00", "observation_type": "isotope_d18o", "unit": "‰", "value": -7.5},
        {"well_code": "W-999", "sampled_at": "2026-09-20T08:00:00+08:00", "observation_type": "solute", "unit": "mg/L", "value": 10},
        {"well_code": "W-100", "sampled_at": "不是时间", "observation_type": "solute", "unit": "mg/L", "value": 10},
        {"well_code": "W-100", "sampled_at": "2026-09-20T08:00:00+08:00", "observation_type": "solute", "unit": "‰", "value": 1},
        {"well_code": "W-100", "sampled_at": "2026-09-20T08:00:00+08:00", "observation_type": "solute", "unit": "mg/L", "value": 250000},
        {"well_code": "W-100", "sampled_at": "2026-09-20T08:00:00+08:00", "observation_type": "isotope_d18o", "unit": "‰", "value": -7.5},
        {"well_code": "W-100", "sampled_at": "2026-09-20T00:00:00Z", "observation_type": "isotope_d18o", "unit": "permil", "value": -7.6},
    ]


def test_submit_validates_and_summarizes_rows(client):
    create_well(client)
    response = submit(client, rows=mixed_rows())
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "staged"
    assert body["replayed"] is False
    assert body["summary"] == {"total": 8, "accepted": 2, "rejected": 4, "converted": 1, "suspected_duplicates": 2}
    errors = {entry["line"]: entry["reason"] for entry in body["errors"]}
    assert set(errors) == {3, 4, 5, 6}
    assert "井点编码不存在" in errors[3]
    assert "采样时间" in errors[4]
    assert "不适用于观测类型" in errors[5]
    assert "超出范围" in errors[6]
    duplicates = {entry["line"] for entry in body["duplicates"]}
    assert duplicates == {7, 8}


def test_resubmit_same_batch_returns_original_result(client):
    create_well(client)
    rows = mixed_rows()
    first = submit(client, rows=rows)
    assert first.status_code == 201
    second = submit(client, rows=rows)
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert second.json()["batch_id"] == first.json()["batch_id"]
    assert second.json()["summary"] == first.json()["summary"]
    conflict = submit(client, rows=[{"well_code": "W-100", "sampled_at": "2026-09-21T00:00:00Z", "observation_type": "solute", "unit": "mg/L", "value": 1}])
    assert conflict.status_code == 409


def test_staging_does_not_pollute_confirmed_data_and_confirm_is_idempotent(client):
    well = create_well(client)
    body = submit(client, rows=mixed_rows()).json()
    batch_id = body["batch_id"]
    before = client.get(f"/api/hydro/wells/{well['id']}/observations")
    assert before.status_code == 200
    assert before.json()["items"] == []
    confirmed = client.post(f"/api/hydro/imports/{batch_id}/confirm")
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "confirmed"
    assert confirmed.json()["imported"] == 2
    items = client.get(f"/api/hydro/wells/{well['id']}/observations").json()["items"]
    assert len(items) == 2
    solute = next(item for item in items if item["observation_type"] == "solute")
    assert solute["value"] == 12.5
    assert solute["unit"] == "mg/L"
    assert solute["detection_limit"] == 0.005
    assert solute["sampled_at"] == "2026-09-20T00:00:00+00:00"
    replay = client.post(f"/api/hydro/imports/{batch_id}/confirm")
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["imported"] == 2
    assert len(client.get(f"/api/hydro/wells/{well['id']}/observations").json()["items"]) == 2


def test_correct_rejected_rows_then_confirm(client):
    well = create_well(client)
    rows = [
        {"well_code": "W-100", "sampled_at": "2026-09-20T08:00:00Z", "observation_type": "solute", "unit": "ppm", "value": 33},
        {"well_code": "W-100", "sampled_at": "2026-09-20T08:00:00Z", "observation_type": "isotope_d2h", "unit": "‰", "value": "abc"},
    ]
    body = submit(client, key="BATCH-FIX", rows=rows).json()
    assert body["summary"]["rejected"] == 2
    batch_id = body["batch_id"]
    fixed = client.patch(f"/api/hydro/imports/{batch_id}/rows", json={"corrections": [
        {"line_number": 1, "fields": {"unit": "mg/L"}},
        {"line_number": 2, "fields": {"value": -52.5}},
        {"line_number": 99, "fields": {"value": 1}},
    ]})
    assert fixed.status_code == 200, fixed.text
    results = {entry["line"]: entry for entry in fixed.json()["results"]}
    assert results[1]["status"] == "accepted"
    assert results[2]["status"] == "accepted"
    assert results[99]["status"] == "error"
    assert fixed.json()["summary"]["accepted"] == 2
    confirmed = client.post(f"/api/hydro/imports/{batch_id}/confirm").json()
    assert confirmed["imported"] == 2
    items = client.get(f"/api/hydro/wells/{well['id']}/observations").json()["items"]
    assert {item["observation_type"] for item in items} == {"solute", "isotope_d2h"}
    again = client.patch(f"/api/hydro/imports/{batch_id}/rows", json={"corrections": [{"line_number": 1, "fields": {"value": 1}}]})
    assert again.status_code == 409


def test_duplicate_against_confirmed_batch_is_flagged_and_skipped(client):
    well = create_well(client)
    row = {"well_code": "W-100", "sampled_at": "2026-09-20T08:00:00Z", "observation_type": "solute", "unit": "mg/L", "value": 30}
    first = submit(client, key="BATCH-A", rows=[row]).json()
    assert first["summary"]["accepted"] == 1
    client.post(f"/api/hydro/imports/{first['batch_id']}/confirm")
    second = submit(client, key="BATCH-B", rows=[row]).json()
    assert second["summary"]["suspected_duplicates"] == 1
    assert second["summary"]["accepted"] == 0
    confirmed = client.post(f"/api/hydro/imports/{second['batch_id']}/confirm").json()
    assert confirmed["imported"] == 0
    assert len(client.get(f"/api/hydro/wells/{well['id']}/observations").json()["items"]) == 1


def test_batch_detail_keeps_raw_snapshot_and_correction(client):
    create_well(client)
    original = {"well_code": "W-100", "sampled_at": "2026-09-20T08:00:00Z", "observation_type": "solute", "unit": "g/L", "value": 0.2, "lab_note": "野外原样"}
    body = submit(client, key="BATCH-SNAPSHOT", rows=[original, {"well_code": "W-100", "sampled_at": "2026-09-21T08:00:00Z", "observation_type": "solute", "unit": "??", "value": 5}]).json()
    batch_id = body["batch_id"]
    client.patch(f"/api/hydro/imports/{batch_id}/rows", json={"corrections": [{"line_number": 2, "fields": {"unit": "mg/L"}}]})
    detail = client.get(f"/api/hydro/imports/{batch_id}")
    assert detail.status_code == 200, detail.text
    rows = {row["line"]: row for row in detail.json()["rows"]}
    assert rows[1]["raw"] == original
    assert rows[1]["normalized"]["value"] == 200.0
    assert rows[1]["normalized"]["unit"] == "mg/L"
    assert rows[1]["normalized"]["converted"] is True
    assert rows[2]["raw"]["unit"] == "??"
    assert rows[2]["correction"] == {"unit": "mg/L"}
    assert rows[2]["status"] == "accepted"


def test_import_validation_requires_rows(client):
    create_well(client)
    assert submit(client, rows=[]).status_code == 422
    missing = client.get("/api/hydro/imports/999")
    assert missing.status_code == 404
    assert client.post("/api/hydro/imports/999/confirm").status_code == 404
