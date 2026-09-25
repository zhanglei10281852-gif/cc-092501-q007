from __future__ import annotations


def create_well(client, code="W-100", **overrides):
    payload = {"code": code, "name": "北部监测井", "latitude": 35.1, "longitude": 116.2,
               "aquifer": "浅层孔隙含水层", "screen_depth_m": 42, **overrides}
    response = client.post("/api/hydro/wells", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def stage(client, source_batch_id="BATCH-2026-001", rows=None, csv_content=None):
    body: dict = {"source_batch_id": source_batch_id}
    if csv_content is not None:
        body["csv_content"] = csv_content
    else:
        body["rows"] = rows or []
    return client.post("/api/hydro/imports", json=body)


def valid_rows(code="W-100", base="S-IMP-"):
    return [
        {"well_code": code, "sample_code": f"{base}1", "sampled_at": "2026-09-20T08:00:00+00:00",
         "observation_type": "d18o", "unit": "‰", "value": -7.5, "detection_limit": 0.05, "measurement_error": 0.1},
        {"well_code": code, "sample_code": f"{base}1", "sampled_at": "2026-09-20T08:00:00+00:00",
         "observation_type": "d2h", "unit": "‰", "value": -52.0, "detection_limit": 0.1, "measurement_error": 0.2},
        # µg/L 应换算成 mg/L
        {"well_code": code, "sample_code": f"{base}1", "sampled_at": "2026-09-20T08:00:00+00:00",
         "observation_type": "solute", "unit": "µg/L", "value": 30000, "detection_limit": 50, "measurement_error": 100},
        {"well_code": code, "sample_code": f"{base}2", "sampled_at": "2026-09-21 09:30:00",
         "observation_type": "solute", "unit": "mg/L", "value": 12.5, "detection_limit": 0.05, "measurement_error": 0.1},
    ]


def test_stage_valid_rows_and_unit_conversion(client):
    create_well(client)
    response = stage(client, rows=valid_rows())
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["status"] == "staged"
    assert data["replayed"] is False
    summary = data["summary"]
    assert summary["total"] == 4
    assert summary["accepted"] == 4
    assert summary["rejected"] == 0
    assert summary["converted"] == 1  # µg/L -> mg/L
    assert summary["suspected_duplicates"] == 0
    solute = next(r for r in data["rows"] if r["observation_type"] == "solute" and r["sample_code"] == "S-IMP-1")
    assert solute["value_canonical"] == 30.0
    assert solute["canonical_unit"] == "mg/L"
    assert solute["converted"] == 1
    assert solute["detection_limit_canonical"] == 0.05
    # 原始字段快照保留
    assert solute["raw"]["unit"] == "µg/L"
    assert solute["raw"]["value"] == 30000
    # 无时区时间被归一化为 UTC
    second = next(r for r in data["rows"] if r["sample_code"] == "S-IMP-2")
    assert second["sampled_at"] == "2026-09-21T09:30:00+00:00"


def test_bad_rows_report_line_numbers_and_reasons(client):
    create_well(client)
    rows = valid_rows()
    rows[1] = {**rows[1], "well_code": "W-404"}                       # 井点不存在
    rows.append({"well_code": "W-100", "sample_code": "S-BAD", "sampled_at": "not-a-time",
                 "observation_type": "d18o", "unit": "‰", "value": "abc"})  # 时间+数值错误
    rows.append({"well_code": "W-100", "sample_code": "S-BAD2", "sampled_at": "2026-09-20T08:00:00+00:00",
                 "observation_type": "nitrate", "unit": "ppm", "value": 1})  # 未知类型+单位
    response = stage(client, source_batch_id="BATCH-BAD", rows=rows)
    assert response.status_code == 201, response.text  # 批次创建成功，坏行走行级错误
    data = response.json()
    assert data["summary"]["accepted"] == 3
    assert data["summary"]["rejected"] == 3
    by_line = {r["row_number"]: r for r in data["rows"]}
    # JSON 数组行号从 1 开始
    assert by_line[2]["status"] == "rejected"
    codes = {e["code"] for e in by_line[2]["errors"]}
    assert "WELL_NOT_FOUND" in codes
    line5 = {e["code"] for e in by_line[5]["errors"]}
    assert {"INVALID_DATETIME", "INVALID_NUMBER"} <= line5
    line6 = {e["code"] for e in by_line[6]["errors"]}
    assert "UNKNOWN_OBSERVATION_TYPE" in line6


def test_csv_import_physical_line_numbers(client):
    create_well(client)
    csv_content = (
        "井点编码,样本编号,采样时间,观测类型,单位,观测值,检测限,误差\n"
        "W-100,S-CSV-1,2026-09-20T08:00:00+00:00,d18o,‰,-7.5,0.05,0.1\n"
        "W-404,S-CSV-2,2026-09-20T08:00:00,d2h,‰,-50,0.1,0.2\n"          # 坏行：物理行 3
    )
    response = stage(client, source_batch_id="BATCH-CSV", csv_content=csv_content)
    assert response.status_code == 201, response.text
    data = response.json()
    by_line = {r["row_number"]: r for r in data["rows"]}
    assert 2 in by_line and by_line[2]["status"] == "staged"
    assert by_line[3]["errors"][0]["code"] == "WELL_NOT_FOUND"


def test_value_range_and_misaligned_columns(client):
    create_well(client)
    rows = [
        {"well_code": "W-100", "sample_code": "S-R", "sampled_at": "2026-09-20T08:00:00+00:00",
         "observation_type": "d18o", "unit": "‰", "value": 250},          # 超范围
        {"well_code": "W-100", "sample_code": "S-R2", "sampled_at": "2026-09-20T08:00:00+00:00",
         "observation_type": "solute", "unit": "mg/L", "value": 5,
         "detection_limit": 0.01, "measurement_error": 5},                # 误差>>检测限
        {"well_code": "W-100", "sample_code": "S-R3", "sampled_at": "2026-09-20T08:00:00+00:00",
         "observation_type": "solute", "unit": "barrels", "value": 5},    # 不支持单位
    ]
    data = stage(client, source_batch_id="BATCH-RANGE", rows=rows).json()
    by_line = {r["row_number"]: r for r in data["rows"]}
    assert "VALUE_OUT_OF_RANGE" in {e["code"] for e in by_line[1]["errors"]}
    assert "SUSPECTED_MISALIGNED_COLUMN" in {e["code"] for e in by_line[2]["errors"]}
    assert "UNSUPPORTED_UNIT" in {e["code"] for e in by_line[3]["errors"]}


def test_duplicate_submission_returns_original_result(client):
    create_well(client)
    first = stage(client, rows=valid_rows())
    assert first.status_code == 201
    first_data = first.json()
    second = stage(client, rows=valid_rows())
    assert second.status_code == 201
    second_data = second.json()
    assert second_data["id"] == first_data["id"]
    assert second_data["replayed"] is True
    # 同批次号但内容不同 -> 冲突
    changed = valid_rows()
    changed[0]["value"] = -9.9
    conflict = stage(client, rows=changed)
    assert conflict.status_code == 422


def test_fix_rows_then_confirm_and_confirmed_data_protected(client):
    create_well(client)
    rows = valid_rows()
    rows[1] = {**rows[1], "well_code": "w-100 "}  # 带空格小写：归一化后应可通过
    rows.append({"well_code": "W-999", "sample_code": "S-FIX", "sampled_at": "2026-09-22T08:00:00+00:00",
                 "observation_type": "d18o", "unit": "‰", "value": -8.0, "detection_limit": 0.05, "measurement_error": 0.1})
    data = stage(client, source_batch_id="BATCH-FIX", rows=rows).json()
    assert data["summary"]["rejected"] == 1  # W-999 坏行；w-100 已归一化通过
    bad_line = 5
    assert data["rows"][4]["status"] == "rejected"

    # 显式点名坏行所在组确认 -> 422 并返回阻塞行号
    blocked = client.post(f"/api/hydro/imports/{data['id']}/confirm", json={"row_numbers": [bad_line]})
    assert blocked.status_code == 422
    assert str(bad_line) in blocked.text

    # 整批确认：干净组先入库，坏组留暂存（partially_confirmed）
    partial = client.post(f"/api/hydro/imports/{data['id']}/confirm", json={})
    assert partial.status_code == 201
    assert partial.json()["status"] == "partially_confirmed"

    # 修正好井号后从暂存状态继续确认
    fixed = client.post(f"/api/hydro/imports/{data['id']}/fix",
                        json={"updates": [{"row_number": bad_line, "well_code": "W-100"}]})
    assert fixed.status_code == 200, fixed.text
    assert fixed.json()["summary"]["rejected"] == 0
    assert fixed.json()["summary"]["accepted"] == 1  # 刚修好的第 5 行
    assert fixed.json()["summary"]["confirmed_rows"] == 4

    confirmed = client.post(f"/api/hydro/imports/{data['id']}/confirm", json={}).json()
    assert confirmed["status"] == "confirmed"
    assert confirmed["summary"]["confirmed_rows"] == 5

    # 数据已入库且单位统一（µg/L -> mg/L）
    well = client.get("/api/hydro/wells/1").json()
    codes = {s["sample_code"]: s for s in well["samples"]}
    sample = codes["S-IMP-1"]
    assert sample["isotope_d18o"] == -7.5
    assert sample["solute_mg_l"] == 30.0
    assert sample["quality_status"] == "usable"

    # 已确认行不可再修改
    again = client.post(f"/api/hydro/imports/{data['id']}/fix",
                        json={"updates": [{"row_number": 1, "value": -1.0}]})
    assert again.status_code == 409


def test_bad_rows_never_pollute_confirmed_data(client):
    create_well(client)
    rows = valid_rows()
    # 第二个样本组里混入坏行
    rows.append({"well_code": "W-100", "sample_code": "S-BROKEN", "sampled_at": "2026-09-22T08:00:00+00:00",
                 "observation_type": "d18o", "unit": "‰", "value": "n/a"})
    data = stage(client, source_batch_id="BATCH-POLLUTE", rows=rows).json()
    # 整体确认：干净样本组入库，坏组留在暂存批次（partially_confirmed）
    partial = client.post(f"/api/hydro/imports/{data['id']}/confirm", json={})
    assert partial.status_code == 201
    assert partial.json()["status"] == "partially_confirmed"
    well = client.get("/api/hydro/wells/1").json()
    assert {s["sample_code"] for s in well["samples"]} == {"S-IMP-1", "S-IMP-2"}

    # 修正坏行后从暂存继续确认
    client.post(f"/api/hydro/imports/{data['id']}/fix",
                json={"updates": [{"row_number": 5, "value": -6.6}]})
    done = client.post(f"/api/hydro/imports/{data['id']}/confirm", json={})
    assert done.status_code == 201, done.text
    assert done.json()["status"] == "confirmed"
    well = client.get("/api/hydro/wells/1").json()
    assert {s["sample_code"] for s in well["samples"]} == {"S-IMP-1", "S-IMP-2", "S-BROKEN"}


def test_suspected_duplicates_flow(client):
    create_well(client)
    data = stage(client, source_batch_id="BATCH-DUP", rows=valid_rows()).json()
    client.post(f"/api/hydro/imports/{data['id']}/confirm", json={})

    # 重新提交同样本编号、同值 -> 疑似重复（库内）
    again = valid_rows(base="S-IMP-")
    again[0]["value"] = -7.5
    data2 = stage(client, source_batch_id="BATCH-DUP2", rows=again).json()
    dup_rows = [r for r in data2["rows"] if r["status"] == "suspected_duplicate"]
    assert dup_rows and all(r["errors"][0]["code"] == "SUSPECTED_DUPLICATE" for r in dup_rows)
    assert data2["summary"]["suspected_duplicates"] >= 1

    # 批内重复：追加两个不同样本的同类型行，与库内已确认样本构成同井同时刻同值疑似重复
    extra = [
        {"well_code": "W-100", "sample_code": "S-INTRA-A", "sampled_at": "2026-09-20T08:00:00+00:00",
         "observation_type": "d18o", "unit": "‰", "value": -7.5, "detection_limit": 0.05, "measurement_error": 0.1},
        {"well_code": "W-100", "sample_code": "S-INTRA-A", "sampled_at": "2026-09-20T08:00:00+00:00",
         "observation_type": "d2h", "unit": "‰", "value": -48.0, "detection_limit": 0.1, "measurement_error": 0.2},
    ]
    data3 = stage(client, source_batch_id="BATCH-INTRA", rows=extra).json()
    assert data3["summary"]["suspected_duplicates"] == 1
    dup_line = next(r["row_number"] for r in data3["rows"] if r["status"] == "suspected_duplicate")
    resolved = client.post(f"/api/hydro/imports/{data3['id']}/resolve-duplicates",
                           json={"resolutions": [{"row_number": dup_line, "resolution": "accepted"}]}).json()
    assert resolved["rows"][-1]["status"] == "staged"
    confirmed = client.post(f"/api/hydro/imports/{data3['id']}/confirm", json={})
    assert confirmed.status_code == 201, confirmed.text


def test_idempotent_confirm_after_partial(client):
    create_well(client)
    data = stage(client, source_batch_id="BATCH-IDEM", rows=valid_rows()).json()
    first = client.post(f"/api/hydro/imports/{data['id']}/confirm",
                        json={"row_numbers": [1, 2, 3]}).json()
    assert first["status"] == "partially_confirmed"
    # 重复提交来源批次返回原结果（包含确认进度）
    replay = stage(client, source_batch_id="BATCH-IDEM", rows=valid_rows()).json()
    assert replay["replayed"] is True
    assert replay["summary"]["confirmed_rows"] == 3
    rest = client.post(f"/api/hydro/imports/{data['id']}/confirm", json={}).json()
    assert rest["status"] == "confirmed"
