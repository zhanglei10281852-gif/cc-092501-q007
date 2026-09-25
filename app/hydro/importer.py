from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timezone
from typing import Any

from app.core.errors import ConflictError
from app.core.security import request_fingerprint
from app.database import get_connection, transaction
from app.hydro.service import ensure_schema as ensure_hydro_schema

SCHEMA = """
CREATE TABLE IF NOT EXISTS hydro_import_batches (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 batch_key TEXT NOT NULL UNIQUE,
 source TEXT NOT NULL,
 request_hash TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'staged' CHECK(status IN ('staged','confirmed')),
 total_rows INTEGER NOT NULL DEFAULT 0,
 accepted_count INTEGER NOT NULL DEFAULT 0,
 rejected_count INTEGER NOT NULL DEFAULT 0,
 converted_count INTEGER NOT NULL DEFAULT 0,
 duplicate_count INTEGER NOT NULL DEFAULT 0,
 result_json TEXT NOT NULL DEFAULT '{}',
 confirm_json TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 confirmed_at TEXT
);
CREATE TABLE IF NOT EXISTS hydro_import_rows (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 batch_id INTEGER NOT NULL REFERENCES hydro_import_batches(id) ON DELETE CASCADE,
 line_number INTEGER NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('accepted','rejected','duplicate')),
 raw_json TEXT NOT NULL,
 correction_json TEXT NOT NULL DEFAULT '',
 error TEXT NOT NULL DEFAULT '',
 well_id INTEGER,
 well_code TEXT NOT NULL DEFAULT '',
 sampled_at TEXT NOT NULL DEFAULT '',
 observation_type TEXT NOT NULL DEFAULT '',
 value_original REAL,
 unit_original TEXT NOT NULL DEFAULT '',
 value_canonical REAL,
 unit_canonical TEXT NOT NULL DEFAULT '',
 converted INTEGER NOT NULL DEFAULT 0 CHECK(converted IN (0,1)),
 detection_limit REAL,
 measurement_error REAL,
 observation_id INTEGER,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 UNIQUE(batch_id, line_number)
);
CREATE TABLE IF NOT EXISTS hydro_observations (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 well_id INTEGER NOT NULL REFERENCES hydro_wells(id) ON DELETE RESTRICT,
 sampled_at TEXT NOT NULL,
 observation_type TEXT NOT NULL,
 value REAL NOT NULL,
 unit TEXT NOT NULL,
 detection_limit REAL,
 measurement_error REAL,
 batch_id INTEGER NOT NULL REFERENCES hydro_import_batches(id) ON DELETE RESTRICT,
 line_number INTEGER NOT NULL,
 created_at TEXT NOT NULL,
 UNIQUE(well_id, sampled_at, observation_type)
);
CREATE INDEX IF NOT EXISTS idx_hydro_import_rows_batch ON hydro_import_rows(batch_id, line_number);
CREATE INDEX IF NOT EXISTS idx_hydro_observations_well ON hydro_observations(well_id, sampled_at);
"""

OBSERVATION_TYPES: dict[str, dict[str, Any]] = {
    "isotope_d18o": {"unit": "‰", "minimum": -100.0, "maximum": 100.0},
    "isotope_d2h": {"unit": "‰", "minimum": -800.0, "maximum": 800.0},
    "solute": {"unit": "mg/L", "minimum": 0.0, "maximum": 100000.0},
}

_UNIT_ALIASES = {
    "‰": "‰",
    "permil": "‰",
    "permille": "‰",
    "per-mille": "‰",
    "mg/l": "mg/L",
    "µg/l": "µg/L",
    "ug/l": "µg/L",
    "g/l": "g/L",
}

_UNIT_FACTORS = {"‰": 1.0, "mg/L": 1.0, "µg/L": 0.001, "g/L": 1000.0}

_ALLOWED_UNITS = {
    "isotope_d18o": {"‰"},
    "isotope_d2h": {"‰"},
    "solute": {"mg/L", "µg/L", "g/L"},
}

DUPLICATE_REASON_CONFIRMED = "与已确认观测记录重复（井点+采样时间+观测类型相同）"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_import_schema() -> None:
    ensure_hydro_schema()
    get_connection().executescript(SCHEMA)


def _normalize_unit(raw: str) -> str | None:
    compact = raw.strip().replace("μ", "µ").replace(" ", "").lower()
    return _UNIT_ALIASES.get(compact)


def _parse_sampled_at(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_number(raw: Any) -> float | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
    elif isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        try:
            value = float(text)
        except ValueError:
            return None
    else:
        return None
    return value if math.isfinite(value) else None


def validate_row(row: Any, wells: dict[str, sqlite3.Row]) -> tuple[dict[str, Any], list[str]]:
    """校验单行并返回 (规范化字段, 错误列表)；有错时规范化字段为空。"""
    if not isinstance(row, dict):
        return {}, ["行记录必须是字段对象"]
    errors: list[str] = []

    raw_well = row.get("well_code")
    well_code = raw_well.strip().upper() if isinstance(raw_well, str) else ""
    well = wells.get(well_code) if well_code else None
    if not well_code:
        errors.append("缺少井点编码 well_code")
    elif well is None:
        errors.append(f"井点编码不存在: {well_code}")

    sampled_at = _parse_sampled_at(row.get("sampled_at"))
    if sampled_at is None:
        errors.append("采样时间 sampled_at 缺失或不是有效的 ISO 8601 时间")

    raw_type = row.get("observation_type")
    observation_type = raw_type.strip().lower() if isinstance(raw_type, str) else ""
    spec = OBSERVATION_TYPES.get(observation_type)
    if spec is None:
        errors.append(f"观测类型不支持: {raw_type!r}，可选 {sorted(OBSERVATION_TYPES)}")

    raw_unit = row.get("unit")
    unit_text = raw_unit.strip() if isinstance(raw_unit, str) else ""
    canonical_unit = _normalize_unit(unit_text) if unit_text else None
    factor: float | None = None
    if not unit_text:
        errors.append("缺少单位 unit")
    elif canonical_unit is None:
        errors.append(f"无法识别的单位: {unit_text}")
    elif spec is not None and canonical_unit not in _ALLOWED_UNITS[observation_type]:
        errors.append(f"单位 {canonical_unit} 不适用于观测类型 {observation_type}")
    else:
        factor = _UNIT_FACTORS[canonical_unit]

    value = _parse_number(row.get("value"))
    if value is None:
        errors.append("数值 value 缺失或不是有效数字")
    canonical_value: float | None = None
    if value is not None and factor is not None:
        canonical_value = round(value * factor, 9)
        if spec is not None and not spec["minimum"] <= canonical_value <= spec["maximum"]:
            errors.append(f"换算后数值 {canonical_value} {spec['unit']} 超出范围 [{spec['minimum']}, {spec['maximum']}]")

    detection_limit: float | None = None
    raw_limit = row.get("detection_limit")
    if raw_limit is not None:
        parsed_limit = _parse_number(raw_limit)
        if parsed_limit is None:
            errors.append("检测限 detection_limit 不是有效数字")
        elif parsed_limit < 0:
            errors.append("检测限 detection_limit 不能为负数")
        elif factor is not None:
            detection_limit = round(parsed_limit * factor, 9)

    measurement_error: float | None = None
    raw_error = row.get("measurement_error")
    if raw_error is not None:
        parsed_error = _parse_number(raw_error)
        if parsed_error is None or not 0 <= parsed_error <= 100:
            errors.append("测量误差 measurement_error 必须是 [0, 100] 内的数字")
        else:
            measurement_error = parsed_error

    if errors:
        return {}, errors
    assert spec is not None and factor is not None and well is not None
    normalized = {
        "well_id": well["id"],
        "well_code": well_code,
        "sampled_at": sampled_at,
        "observation_type": observation_type,
        "value_original": value,
        "unit_original": unit_text,
        "value_canonical": canonical_value,
        "unit_canonical": spec["unit"],
        "converted": 1 if factor != 1.0 else 0,
        "detection_limit": detection_limit,
        "measurement_error": measurement_error,
    }
    return normalized, []


class HydroImportService:
    def __init__(self, connection: sqlite3.Connection | None = None):
        self.connection = connection or get_connection()
        ensure_import_schema()

    def _wells_by_code(self, connection: sqlite3.Connection) -> dict[str, sqlite3.Row]:
        rows = connection.execute("SELECT * FROM hydro_wells").fetchall()
        return {row["code"].strip().upper(): row for row in rows}

    def _confirmed_keys(self, connection: sqlite3.Connection) -> set[tuple[int, str, str]]:
        rows = connection.execute("SELECT well_id,sampled_at,observation_type FROM hydro_observations").fetchall()
        return {(row["well_id"], row["sampled_at"], row["observation_type"]) for row in rows}

    def _require_batch(self, batch_id: int) -> sqlite3.Row:
        batch = self.connection.execute("SELECT * FROM hydro_import_batches WHERE id=?", (batch_id,)).fetchone()
        if batch is None:
            raise KeyError("import_batch_not_found")
        return batch

    @staticmethod
    def _replay(batch: sqlite3.Row, fingerprint: str) -> tuple[dict[str, Any], int]:
        if batch["request_hash"] != fingerprint:
            raise ConflictError("相同批次键提交了不同的导入内容")
        stored = json.loads(batch["result_json"])
        stored["replayed"] = True
        return stored, 200

    @staticmethod
    def _summarize(records: list[dict[str, Any]]) -> dict[str, int]:
        return {
            "total": len(records),
            "accepted": sum(1 for r in records if r["status"] == "accepted"),
            "rejected": sum(1 for r in records if r["status"] == "rejected"),
            "converted": sum(1 for r in records if r["status"] == "accepted" and r["normalized"].get("converted")),
            "suspected_duplicates": sum(1 for r in records if r["status"] == "duplicate"),
        }

    def _recount(self, connection: sqlite3.Connection, batch_id: int) -> dict[str, int]:
        rows = connection.execute("SELECT status,converted FROM hydro_import_rows WHERE batch_id=?", (batch_id,)).fetchall()
        summary = {"total": len(rows), "accepted": 0, "rejected": 0, "converted": 0, "suspected_duplicates": 0}
        for row in rows:
            if row["status"] == "accepted":
                summary["accepted"] += 1
                summary["converted"] += row["converted"]
            elif row["status"] == "rejected":
                summary["rejected"] += 1
            else:
                summary["suspected_duplicates"] += 1
        connection.execute(
            "UPDATE hydro_import_batches SET total_rows=?,accepted_count=?,rejected_count=?,converted_count=?,duplicate_count=?,updated_at=? WHERE id=?",
            (summary["total"], summary["accepted"], summary["rejected"], summary["converted"], summary["suspected_duplicates"], _now(), batch_id),
        )
        return summary

    def submit_batch(self, payload: dict[str, Any], actor: str = "researcher") -> tuple[dict[str, Any], int]:
        batch_key = payload["batch_key"]
        source = payload["source"]
        rows = payload["rows"]
        fingerprint = request_fingerprint({"batch_key": batch_key, "source": source, "rows": rows})
        existing = self.connection.execute("SELECT * FROM hydro_import_batches WHERE batch_key=?", (batch_key,)).fetchone()
        if existing is not None:
            return self._replay(existing, fingerprint)

        wells = self._wells_by_code(self.connection)
        confirmed = self._confirmed_keys(self.connection)
        staged: list[dict[str, Any]] = []
        seen: dict[tuple[int, str, str], int] = {}
        for index, row in enumerate(rows, start=1):
            normalized, errors = validate_row(row, wells)
            record = {
                "line_number": index,
                "raw": row if isinstance(row, dict) else {"_raw": row},
                "normalized": normalized,
                "errors": errors,
                "status": "rejected" if errors else "accepted",
            }
            if not errors:
                key = (normalized["well_id"], normalized["sampled_at"], normalized["observation_type"])
                if key in confirmed:
                    record["status"] = "duplicate"
                    record["errors"] = [DUPLICATE_REASON_CONFIRMED]
                elif key in seen:
                    record["status"] = "duplicate"
                    record["errors"] = [f"与本批次第 {seen[key]} 行重复（井点+采样时间+观测类型相同）"]
                else:
                    seen[key] = index
            staged.append(record)

        summary = self._summarize(staged)
        body: dict[str, Any] = {
            "batch_key": batch_key,
            "source": source,
            "status": "staged",
            "summary": summary,
            "errors": [{"line": r["line_number"], "reason": "；".join(r["errors"])} for r in staged if r["status"] == "rejected"],
            "duplicates": [{"line": r["line_number"], "reason": "；".join(r["errors"])} for r in staged if r["status"] == "duplicate"],
            "replayed": False,
        }
        now = _now()
        with transaction(immediate=True) as connection:
            try:
                cursor = connection.execute(
                    "INSERT INTO hydro_import_batches(batch_key,source,request_hash,status,total_rows,accepted_count,rejected_count,converted_count,duplicate_count,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (batch_key, source, fingerprint, "staged", summary["total"], summary["accepted"], summary["rejected"], summary["converted"], summary["suspected_duplicates"], now, now),
                )
            except sqlite3.IntegrityError:
                concurrent = connection.execute("SELECT * FROM hydro_import_batches WHERE batch_key=?", (batch_key,)).fetchone()
                return self._replay(concurrent, fingerprint)
            batch_id = cursor.lastrowid
            for record in staged:
                n = record["normalized"]
                connection.execute(
                    "INSERT INTO hydro_import_rows(batch_id,line_number,status,raw_json,error,well_id,well_code,sampled_at,observation_type,value_original,unit_original,value_canonical,unit_canonical,converted,detection_limit,measurement_error,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        batch_id, record["line_number"], record["status"], json.dumps(record["raw"], ensure_ascii=False),
                        "；".join(record["errors"]), n.get("well_id"), n.get("well_code", ""), n.get("sampled_at", ""),
                        n.get("observation_type", ""), n.get("value_original"), n.get("unit_original", ""),
                        n.get("value_canonical"), n.get("unit_canonical", ""), n.get("converted", 0),
                        n.get("detection_limit"), n.get("measurement_error"), now, now,
                    ),
                )
            body["batch_id"] = batch_id
            connection.execute("UPDATE hydro_import_batches SET result_json=? WHERE id=?", (json.dumps(body, ensure_ascii=False), batch_id))
            connection.execute(
                "INSERT INTO hydro_audit(resource_type,resource_id,action,actor,payload_json,created_at) VALUES('import_batch',?,?,?,?,?)",
                (batch_id, "create", actor, json.dumps({"batch_key": batch_key, "summary": summary}, ensure_ascii=False), now),
            )
        return body, 201

    def list_batches(self, limit: int = 50) -> dict[str, Any]:
        rows = self.connection.execute("SELECT * FROM hydro_import_batches ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return {"items": [self._batch_view(row) for row in rows]}

    def get_batch(self, batch_id: int) -> dict[str, Any] | None:
        batch = self.connection.execute("SELECT * FROM hydro_import_batches WHERE id=?", (batch_id,)).fetchone()
        if batch is None:
            return None
        result = self._batch_view(batch)
        rows = self.connection.execute("SELECT * FROM hydro_import_rows WHERE batch_id=? ORDER BY line_number", (batch_id,)).fetchall()
        result["rows"] = [self._row_view(row) for row in rows]
        return result

    @staticmethod
    def _batch_view(batch: sqlite3.Row) -> dict[str, Any]:
        return {
            "batch_id": batch["id"],
            "batch_key": batch["batch_key"],
            "source": batch["source"],
            "status": batch["status"],
            "summary": {
                "total": batch["total_rows"],
                "accepted": batch["accepted_count"],
                "rejected": batch["rejected_count"],
                "converted": batch["converted_count"],
                "suspected_duplicates": batch["duplicate_count"],
            },
            "created_at": batch["created_at"],
            "updated_at": batch["updated_at"],
            "confirmed_at": batch["confirmed_at"],
        }

    @staticmethod
    def _row_view(row: sqlite3.Row) -> dict[str, Any]:
        normalized = None
        if row["status"] != "rejected":
            normalized = {
                "well_code": row["well_code"],
                "sampled_at": row["sampled_at"],
                "observation_type": row["observation_type"],
                "value": row["value_canonical"],
                "unit": row["unit_canonical"],
                "value_original": row["value_original"],
                "unit_original": row["unit_original"],
                "converted": bool(row["converted"]),
                "detection_limit": row["detection_limit"],
                "measurement_error": row["measurement_error"],
            }
        return {
            "line": row["line_number"],
            "status": row["status"],
            "error": row["error"],
            "raw": json.loads(row["raw_json"]),
            "correction": json.loads(row["correction_json"]) if row["correction_json"] else None,
            "normalized": normalized,
            "observation_id": row["observation_id"],
        }

    def _resweep_duplicates(self, connection: sqlite3.Connection, batch_id: int) -> None:
        confirmed = self._confirmed_keys(connection)
        rows = connection.execute(
            "SELECT id,line_number,status,well_id,sampled_at,observation_type FROM hydro_import_rows WHERE batch_id=? AND status IN ('accepted','duplicate') ORDER BY line_number",
            (batch_id,),
        ).fetchall()
        seen: dict[tuple[int, str, str], int] = {}
        now = _now()
        for row in rows:
            key = (row["well_id"], row["sampled_at"], row["observation_type"])
            if key in confirmed:
                status, message = "duplicate", DUPLICATE_REASON_CONFIRMED
            elif key in seen:
                status, message = "duplicate", f"与本批次第 {seen[key]} 行重复（井点+采样时间+观测类型相同）"
            else:
                seen[key] = row["line_number"]
                status, message = "accepted", ""
            if status != row["status"] or message:
                connection.execute("UPDATE hydro_import_rows SET status=?, error=?, updated_at=? WHERE id=?", (status, message, now, row["id"]))

    def correct_rows(self, batch_id: int, corrections: list[dict[str, Any]], actor: str = "researcher") -> dict[str, Any]:
        batch = self._require_batch(batch_id)
        if batch["status"] != "staged":
            raise ConflictError("批次已确认，不能再修正")
        results: list[dict[str, Any]] = []
        with transaction(immediate=True) as connection:
            wells = self._wells_by_code(connection)
            now = _now()
            for correction in corrections:
                line = correction["line_number"]
                fields = correction["fields"]
                row = connection.execute("SELECT * FROM hydro_import_rows WHERE batch_id=? AND line_number=?", (batch_id, line)).fetchone()
                if row is None:
                    results.append({"line": line, "status": "error", "reason": "行号不存在"})
                    continue
                if row["observation_id"] is not None:
                    results.append({"line": line, "status": "error", "reason": "该行已入库，不能修正"})
                    continue
                merged = {**json.loads(row["raw_json"]), **fields}
                normalized, errors = validate_row(merged, wells)
                status = "rejected" if errors else "accepted"
                message = "；".join(errors)
                connection.execute(
                    "UPDATE hydro_import_rows SET status=?, error=?, correction_json=?, well_id=?, well_code=?, sampled_at=?, observation_type=?, value_original=?, unit_original=?, value_canonical=?, unit_canonical=?, converted=?, detection_limit=?, measurement_error=?, updated_at=? WHERE id=?",
                    (
                        status, message, json.dumps(fields, ensure_ascii=False), normalized.get("well_id"),
                        normalized.get("well_code", ""), normalized.get("sampled_at", ""), normalized.get("observation_type", ""),
                        normalized.get("value_original"), normalized.get("unit_original", ""), normalized.get("value_canonical"),
                        normalized.get("unit_canonical", ""), normalized.get("converted", 0), normalized.get("detection_limit"),
                        normalized.get("measurement_error"), now, row["id"],
                    ),
                )
                results.append({"line": line, "status": status, "reason": message})
            self._resweep_duplicates(connection, batch_id)
            for entry in results:
                if entry["status"] == "accepted":
                    final = connection.execute("SELECT status,error FROM hydro_import_rows WHERE batch_id=? AND line_number=?", (batch_id, entry["line"])).fetchone()
                    entry["status"] = final["status"]
                    entry["reason"] = final["error"]
            summary = self._recount(connection, batch_id)
            connection.execute(
                "INSERT INTO hydro_audit(resource_type,resource_id,action,actor,payload_json,created_at) VALUES('import_batch',?,?,?,?,?)",
                (batch_id, "correct", actor, json.dumps({"lines": [c["line_number"] for c in corrections]}, ensure_ascii=False), now),
            )
        return {"batch_id": batch_id, "status": "staged", "summary": summary, "results": results}

    def confirm_batch(self, batch_id: int, actor: str = "researcher") -> dict[str, Any]:
        batch = self._require_batch(batch_id)
        if batch["status"] == "confirmed":
            stored = json.loads(batch["confirm_json"])
            stored["replayed"] = True
            return stored
        now = _now()
        with transaction(immediate=True) as connection:
            current = connection.execute("SELECT * FROM hydro_import_batches WHERE id=?", (batch_id,)).fetchone()
            if current["status"] == "confirmed":
                stored = json.loads(current["confirm_json"])
                stored["replayed"] = True
                return stored
            rows = connection.execute("SELECT * FROM hydro_import_rows WHERE batch_id=? AND status='accepted' ORDER BY line_number", (batch_id,)).fetchall()
            imported = 0
            for row in rows:
                if connection.execute("SELECT id FROM hydro_wells WHERE id=?", (row["well_id"],)).fetchone() is None:
                    connection.execute("UPDATE hydro_import_rows SET status='rejected', error='井点已被删除', updated_at=? WHERE id=?", (now, row["id"]))
                    continue
                clash = connection.execute(
                    "SELECT id FROM hydro_observations WHERE well_id=? AND sampled_at=? AND observation_type=?",
                    (row["well_id"], row["sampled_at"], row["observation_type"]),
                ).fetchone()
                if clash is not None:
                    connection.execute("UPDATE hydro_import_rows SET status='duplicate', error=?, updated_at=? WHERE id=?", (DUPLICATE_REASON_CONFIRMED, now, row["id"]))
                    continue
                cursor = connection.execute(
                    "INSERT INTO hydro_observations(well_id,sampled_at,observation_type,value,unit,detection_limit,measurement_error,batch_id,line_number,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (row["well_id"], row["sampled_at"], row["observation_type"], row["value_canonical"], row["unit_canonical"], row["detection_limit"], row["measurement_error"], batch_id, row["line_number"], now),
                )
                connection.execute("UPDATE hydro_import_rows SET observation_id=?, updated_at=? WHERE id=?", (cursor.lastrowid, now, row["id"]))
                imported += 1
            summary = self._recount(connection, batch_id)
            body = {"batch_id": batch_id, "status": "confirmed", "imported": imported, "summary": summary, "confirmed_at": now, "replayed": False}
            connection.execute("UPDATE hydro_import_batches SET status='confirmed', confirmed_at=?, confirm_json=?, updated_at=? WHERE id=?", (now, json.dumps(body, ensure_ascii=False), now, batch_id))
            connection.execute(
                "INSERT INTO hydro_audit(resource_type,resource_id,action,actor,payload_json,created_at) VALUES('import_batch',?,?,?,?,?)",
                (batch_id, "confirm", actor, json.dumps({"imported": imported}, ensure_ascii=False), now),
            )
        return body

    def list_observations(self, well_id: int) -> dict[str, Any]:
        if self.connection.execute("SELECT id FROM hydro_wells WHERE id=?", (well_id,)).fetchone() is None:
            raise KeyError("well_not_found")
        rows = self.connection.execute("SELECT * FROM hydro_observations WHERE well_id=? ORDER BY sampled_at,id", (well_id,)).fetchall()
        return {"well_id": well_id, "items": [dict(row) for row in rows]}
