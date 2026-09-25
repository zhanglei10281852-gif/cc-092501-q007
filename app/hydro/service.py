from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import datetime, timezone
from typing import Any

from app.database import get_connection, transaction
from app.core.errors import ValidationError


SCHEMA = """
CREATE TABLE IF NOT EXISTS hydro_wells (
 id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
 latitude REAL NOT NULL, longitude REAL NOT NULL, aquifer TEXT NOT NULL, screen_depth_m REAL NOT NULL,
 status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hydro_endmembers (
 id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, isotope_d18o REAL NOT NULL,
 isotope_d2h REAL NOT NULL, solute_mg_l REAL NOT NULL, uncertainty REAL NOT NULL,
 version TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)), created_at TEXT NOT NULL,
 UNIQUE(name,version)
);
CREATE TABLE IF NOT EXISTS hydro_samples (
 id INTEGER PRIMARY KEY AUTOINCREMENT, well_id INTEGER NOT NULL REFERENCES hydro_wells(id) ON DELETE RESTRICT,
 sample_code TEXT NOT NULL UNIQUE, sampled_at TEXT NOT NULL, isotope_d18o REAL, isotope_d2h REAL,
 solute_mg_l REAL, detection_limit REAL NOT NULL, measurement_error REAL NOT NULL,
 quality_status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hydro_inversions (
 id INTEGER PRIMARY KEY AUTOINCREMENT, sample_id INTEGER NOT NULL REFERENCES hydro_samples(id) ON DELETE RESTRICT,
 task_key TEXT NOT NULL UNIQUE, model_version TEXT NOT NULL, method TEXT NOT NULL,
 input_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
 worker_id TEXT NOT NULL DEFAULT '', result_json TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hydro_transport_runs (
 id INTEGER PRIMARY KEY AUTOINCREMENT, well_id INTEGER NOT NULL REFERENCES hydro_wells(id) ON DELETE RESTRICT,
 task_key TEXT NOT NULL UNIQUE, model_version TEXT NOT NULL, input_json TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'queued', result_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hydro_audit (
 id INTEGER PRIMARY KEY AUTOINCREMENT, resource_type TEXT NOT NULL, resource_id INTEGER,
 action TEXT NOT NULL, actor TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hydro_samples_well ON hydro_samples(well_id,sampled_at);
CREATE INDEX IF NOT EXISTS idx_hydro_inversions_status ON hydro_inversions(status,created_at);
CREATE TABLE IF NOT EXISTS hydro_import_batches (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 source_batch_id TEXT NOT NULL UNIQUE,
 status TEXT NOT NULL DEFAULT 'staged' CHECK(status IN ('staged','partially_confirmed','confirmed')),
 request_hash TEXT NOT NULL,
 summary_json TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL, confirmed_at TEXT
);
CREATE TABLE IF NOT EXISTS hydro_import_rows (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 batch_id INTEGER NOT NULL REFERENCES hydro_import_batches(id) ON DELETE CASCADE,
 row_number INTEGER NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('staged','suspected_duplicate','rejected','confirmed')),
 error_code TEXT NOT NULL DEFAULT '',
 error_json TEXT NOT NULL DEFAULT '[]',
 well_code TEXT NOT NULL DEFAULT '', sample_code TEXT NOT NULL DEFAULT '',
 sampled_at TEXT NOT NULL DEFAULT '', observation_type TEXT NOT NULL DEFAULT '',
 source_unit TEXT NOT NULL DEFAULT '', source_value REAL,
 converted INTEGER NOT NULL DEFAULT 0 CHECK(converted IN (0,1)),
 canonical_unit TEXT NOT NULL DEFAULT '',
 value_canonical REAL, detection_limit_canonical REAL, measurement_error_canonical REAL,
 raw_json TEXT NOT NULL, payload_json TEXT NOT NULL,
 sample_id INTEGER REFERENCES hydro_samples(id),
 duplicate_resolution TEXT NOT NULL DEFAULT '' CHECK(duplicate_resolution IN ('','accepted','rejected')),
 UNIQUE(batch_id,row_number)
);
CREATE INDEX IF NOT EXISTS idx_hydro_import_rows_batch ON hydro_import_rows(batch_id,status);
"""

IMPORT_ROW_LIMIT = 1000

# 观测类型 -> 宽表列、标准单位、允许单位及换算系数、标准单位下的合理范围
OBSERVATION_TYPES: dict[str, dict[str, Any]] = {
    "d18o": {"column": "isotope_d18o", "canonical_unit": "‰",
             "units": {"‰": 1.0, "permil": 1.0}, "min": -100.0, "max": 100.0},
    "d2h": {"column": "isotope_d2h", "canonical_unit": "‰",
            "units": {"‰": 1.0, "permil": 1.0}, "min": -800.0, "max": 800.0},
    "solute": {"column": "solute_mg_l", "canonical_unit": "mg/L",
               "units": {"mg/l": 1.0, "µg/l": 0.001, "ug/l": 0.001}, "min": 0.0, "max": 100000.0},
}
OBSERVATION_ALIASES = {
    "d18o": "d18o", "isotope_d18o": "d18o", "delta18o": "d18o", "δ18o": "d18o", "o18": "d18o",
    "d2h": "d2h", "isotope_d2h": "d2h", "delta2h": "d2h", "δ2h": "d2h", "dd": "d2h",
    "solute": "solute", "solute_mg_l": "solute", "concentration": "solute", "浓度": "solute",
}
DETECTION_LIMIT_MAX = 100000.0
MEASUREMENT_ERROR_MAX = 100.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_schema() -> None:
    get_connection().executescript(SCHEMA)


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


class HydroService:
    def __init__(self, connection: sqlite3.Connection | None = None):
        self.connection = connection or get_connection()
        ensure_schema()

    def create_well(self, payload: dict[str, Any], actor: str = "researcher") -> dict[str, Any]:
        now = _now()
        with transaction(immediate=True) as connection:
            cursor = connection.execute("INSERT INTO hydro_wells(code,name,latitude,longitude,aquifer,screen_depth_m,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", (payload["code"],payload["name"],payload["latitude"],payload["longitude"],payload["aquifer"],payload["screen_depth_m"],now,now))
            well_id = cursor.lastrowid
            connection.execute("INSERT INTO hydro_audit(resource_type,resource_id,action,actor,payload_json,created_at) VALUES('well',?,?,?,?,?)", (well_id,"create",actor,json.dumps(payload,ensure_ascii=False),now))
            return dict(connection.execute("SELECT * FROM hydro_wells WHERE id=?",(well_id,)).fetchone())

    def get_well(self, well_id: int) -> dict[str, Any] | None:
        well = self.connection.execute("SELECT * FROM hydro_wells WHERE id=?",(well_id,)).fetchone()
        if well is None: return None
        result = dict(well)
        result["samples"] = [dict(r) for r in self.connection.execute("SELECT * FROM hydro_samples WHERE well_id=? ORDER BY sampled_at,id",(well_id,)).fetchall()]
        return result

    def delete_well(self, well_id: int) -> bool:
        with transaction(immediate=True) as connection:
            cursor = connection.execute("DELETE FROM hydro_wells WHERE id=?",(well_id,))
            if cursor.rowcount == 0: raise KeyError("well_not_found")
            return True

    def create_endmember(self, payload: dict[str, Any]) -> dict[str, Any]:
        now=_now()
        with transaction(immediate=True) as connection:
            cursor=connection.execute("INSERT INTO hydro_endmembers(name,isotope_d18o,isotope_d2h,solute_mg_l,uncertainty,version,created_at) VALUES(?,?,?,?,?,?,?)",(payload["name"],payload["isotope_d18o"],payload["isotope_d2h"],payload["solute_mg_l"],payload["uncertainty"],payload["version"],now))
            return dict(connection.execute("SELECT * FROM hydro_endmembers WHERE id=?",(cursor.lastrowid,)).fetchone())

    def add_sample(self, well_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        if self.connection.execute("SELECT id FROM hydro_wells WHERE id=?",(well_id,)).fetchone() is None: raise KeyError("well_not_found")
        values=[payload.get("isotope_d18o"),payload.get("isotope_d2h"),payload.get("solute_mg_l")]
        quality="usable" if sum(v is not None for v in values)>=2 else "incomplete"
        now=_now()
        with transaction(immediate=True) as connection:
            cursor=connection.execute("INSERT INTO hydro_samples(well_id,sample_code,sampled_at,isotope_d18o,isotope_d2h,solute_mg_l,detection_limit,measurement_error,quality_status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",(well_id,payload["sample_code"],payload["sampled_at"],payload.get("isotope_d18o"),payload.get("isotope_d2h"),payload.get("solute_mg_l"),payload["detection_limit"],payload["measurement_error"],quality,now))
            return dict(connection.execute("SELECT * FROM hydro_samples WHERE id=?",(cursor.lastrowid,)).fetchone())

    def _project_simplex(self, values: list[float]) -> list[float]:
        clipped=[max(0.0,v) for v in values]
        total=sum(clipped)
        return [1/len(values)]*len(values) if total<=1e-15 else [v/total for v in clipped]

    def solve_mixture(self, sample: sqlite3.Row, endmembers: list[sqlite3.Row], max_iterations: int, tolerance: float) -> dict[str, Any]:
        observed=[sample["isotope_d18o"],sample["isotope_d2h"],sample["solute_mg_l"]]
        active=[i for i,v in enumerate(observed) if v is not None]
        if len(active)<2: raise ValueError("insufficient_measurements")
        fractions=[1/len(endmembers)]*len(endmembers)
        scale=[20.0,100.0,max(1.0,float(sample["solute_mg_l"] or 1))]
        rate=0.08
        last=float("inf")
        for iteration in range(max_iterations):
            predicted=[sum(fractions[j]*[e["isotope_d18o"],e["isotope_d2h"],e["solute_mg_l"]][k] for j,e in enumerate(endmembers)) for k in range(3)]
            residual=[(predicted[k]-float(observed[k]))/scale[k] if k in active else 0.0 for k in range(3)]
            objective=sum(r*r for r in residual)+((sum(fractions)-1.0)*10)**2
            if abs(last-objective)<tolerance: break
            last=objective
            gradient=[]
            for e in endmembers:
                vector=[e["isotope_d18o"],e["isotope_d2h"],e["solute_mg_l"]]
                gradient.append(2*sum(residual[k]*vector[k]/scale[k] for k in active))
            fractions=self._project_simplex([f-rate*g for f,g in zip(fractions,gradient)])
        predicted=[sum(fractions[j]*[e["isotope_d18o"],e["isotope_d2h"],e["solute_mg_l"]][k] for j,e in enumerate(endmembers)) for k in range(3)]
        rmse=math.sqrt(sum(((predicted[k]-float(observed[k]))/scale[k])**2 for k in active)/len(active))
        return {"fractions":[{"endmember_id":e["id"],"name":e["name"],"fraction":round(f,8)} for e,f in zip(endmembers,fractions)],"mass_balance":round(sum(fractions),10),"predicted":predicted,"rmse":rmse,"iterations":iteration+1,"converged":abs(last-objective)<tolerance}

    def enqueue_inversion(self, sample_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        sample=self.connection.execute("SELECT * FROM hydro_samples WHERE id=?",(sample_id,)).fetchone()
        if sample is None: raise KeyError("sample_not_found")
        ids=sorted(set(payload["endmember_ids"]))
        endmembers=self.connection.execute(f"SELECT * FROM hydro_endmembers WHERE active=1 AND id IN ({','.join('?' for _ in ids)}) ORDER BY id",ids).fetchall()
        if len(endmembers)!=len(ids): raise ValueError("endmember_not_found")
        input_data={**payload,"endmember_ids":ids,"sample":dict(sample),"endmembers":[dict(e) for e in endmembers]}
        key=_digest(input_data); now=_now()
        with transaction(immediate=True) as connection:
            old=connection.execute("SELECT * FROM hydro_inversions WHERE task_key=?",(key,)).fetchone()
            if old: return dict(old)
            cursor=connection.execute("INSERT INTO hydro_inversions(sample_id,task_key,model_version,method,input_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",(sample_id,key,payload["model_version"],payload["method"],json.dumps(input_data,ensure_ascii=False),now,now))
            return dict(connection.execute("SELECT * FROM hydro_inversions WHERE id=?",(cursor.lastrowid,)).fetchone())

    def run_inversion(self, task_id: int, worker_id: str) -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            task=connection.execute("SELECT * FROM hydro_inversions WHERE id=?",(task_id,)).fetchone()
            if task is None: raise KeyError("task_not_found")
            if task["status"]=="done": return dict(task)
            connection.execute("UPDATE hydro_inversions SET status='running',attempts=attempts+1,worker_id=?,updated_at=? WHERE id=?",(worker_id,_now(),task_id))
        data=json.loads(task["input_json"])
        sample=self.connection.execute("SELECT * FROM hydro_samples WHERE id=?",(task["sample_id"],)).fetchone()
        ids=data["endmember_ids"]
        endmembers=self.connection.execute(f"SELECT * FROM hydro_endmembers WHERE id IN ({','.join('?' for _ in ids)}) ORDER BY id",ids).fetchall()
        try: result=self.solve_mixture(sample,endmembers,data["max_iterations"],data["tolerance"])
        except Exception as exc:
            with transaction(immediate=True) as connection: connection.execute("UPDATE hydro_inversions SET status='failed',error=?,updated_at=? WHERE id=?",(str(exc),_now(),task_id))
            raise
        with transaction(immediate=True) as connection:
            connection.execute("UPDATE hydro_inversions SET status='done',result_json=?,error='',updated_at=? WHERE id=?",(json.dumps(result,ensure_ascii=False),_now(),task_id))
            return dict(connection.execute("SELECT * FROM hydro_inversions WHERE id=?",(task_id,)).fetchone())

    def run_transport(self, well_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        if self.connection.execute("SELECT id FROM hydro_wells WHERE id=?",(well_id,)).fetchone() is None: raise KeyError("well_not_found")
        key=_digest({"well_id":well_id,**payload}); now=_now()
        old=self.connection.execute("SELECT * FROM hydro_transport_runs WHERE task_key=?",(key,)).fetchone()
        if old: return dict(old)
        points=[]; t=payload["step_days"]
        while t<=payload["duration_days"]+1e-12:
            d=payload["dispersion_m2_day"]; x=payload["distance_m"]; v=payload["velocity_m_day"]
            c=payload["source_concentration"]*math.exp(-((x-v*t)**2)/(4*d*t))*math.exp(-payload["decay_per_day"]*t)/math.sqrt(4*math.pi*d*t)
            points.append({"time_days":round(t,8),"concentration":c}); t+=payload["step_days"]
        peak=max(points,key=lambda p:p["concentration"])
        result={"points":points,"peak":peak,"arrival_time_days":payload["distance_m"]/payload["velocity_m_day"],"model_version":payload["model_version"]}
        with transaction(immediate=True) as connection:
            cursor=connection.execute("INSERT INTO hydro_transport_runs(well_id,task_key,model_version,input_json,status,result_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",(well_id,key,payload["model_version"],json.dumps(payload,ensure_ascii=False),"done",json.dumps(result,ensure_ascii=False),now,now))
            return dict(connection.execute("SELECT * FROM hydro_transport_runs WHERE id=?",(cursor.lastrowid,)).fetchone())

    # ------------------------------------------------------------------
    # 批量导入：解析校验 -> 暂存 -> 修正 -> 确认入库
    # ------------------------------------------------------------------

    IMPORT_HEADER_ALIASES = {
        "well_code": ("well_code", "well", "井点编码", "井号", "井点编号"),
        "sample_code": ("sample_code", "sample", "样本编号", "样品编号"),
        "sampled_at": ("sampled_at", "sample_time", "采样时间"),
        "observation_type": ("observation_type", "type", "观测类型", "指标"),
        "unit": ("unit", "units", "单位"),
        "value": ("value", "观测值", "数值", "结果"),
        "detection_limit": ("detection_limit", "检测限", "检出限"),
        "measurement_error": ("measurement_error", "误差", "测量误差", "不确定度"),
    }
    REQUIRED_FIELDS = ("well_code", "sample_code", "sampled_at", "observation_type", "value")

    @staticmethod
    def _to_float(value: Any) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValueError
        if isinstance(value, (int, float)):
            number = float(value)
        else:
            text = str(value).strip().replace(",", "")
            if text == "":
                return None
            number = float(text)
        if not math.isfinite(number):
            raise ValueError
        return number

    @staticmethod
    def _parse_sampled_at(value: Any) -> str:
        if value is None:
            raise ValueError
        text = str(value).strip()
        if not text:
            raise ValueError
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        text = text.replace("/", "-")
        if " " in text and "T" not in text:
            text = text.replace(" ", "T", 1)
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")

    def _parse_import_payload(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        if payload.get("rows"):
            return [dict(row, _row_number=index + 1) for index, row in enumerate(payload["rows"])]
        import csv
        import io
        content = payload.get("csv_content")
        if not content:
            raise ValueError("import_rows_required")
        text = content.lstrip("﻿")
        reader = csv.DictReader(io.StringIO(text))
        aliases = {alias.strip().lower(): field for field, names in self.IMPORT_HEADER_ALIASES.items() for alias in names}
        mapped_header = {}
        for column in reader.fieldnames or ():
            mapped_header[column] = aliases.get(column.strip().lower())
        rows: list[dict[str, Any]] = []
        for offset, raw in enumerate(reader):
            row: dict[str, Any] = {"_row_number": offset + 2}  # 含表头的物理行号
            for column, cell in raw.items():
                field = mapped_header.get(column)
                if field is not None and cell is not None:
                    row[field] = cell
            rows.append(row)
        if not rows:
            raise ValueError("import_rows_required")
        if len(rows) > IMPORT_ROW_LIMIT:
            raise ValueError("import_too_many_rows")
        return rows

    def _validate_row(self, connection: sqlite3.Connection, raw: dict[str, Any]) -> dict[str, Any]:
        errors: list[dict[str, str]] = []
        fields = {key: raw.get(key) for key in
                  ("well_code", "sample_code", "sampled_at", "observation_type", "unit",
                   "value", "detection_limit", "measurement_error")}

        well_code = str(fields["well_code"]).strip().upper() if fields["well_code"] not in (None, "") else ""
        sample_code = str(fields["sample_code"]).strip() if fields["sample_code"] not in (None, "") else ""
        for name in ("well_code", "sample_code", "sampled_at", "observation_type", "value"):
            if fields[name] in (None, ""):
                errors.append({"code": "MISSING_FIELD", "message": f"缺少必填字段 {name}"})

        well_id: int | None = None
        if well_code:
            well = connection.execute("SELECT id FROM hydro_wells WHERE code=?", (well_code,)).fetchone()
            if well is None:
                errors.append({"code": "WELL_NOT_FOUND", "message": f"井点编码不存在: {well_code}"})
            else:
                well_id = well["id"]

        sampled_at = ""
        if fields["sampled_at"] not in (None, ""):
            try:
                sampled_at = self._parse_sampled_at(fields["sampled_at"])
            except (ValueError, OverflowError):
                errors.append({"code": "INVALID_DATETIME",
                               "message": f"采样时间无法解析: {fields['sampled_at']!r}"})

        obs_type = ""
        spec: dict[str, Any] | None = None
        if fields["observation_type"] not in (None, ""):
            obs_type = OBSERVATION_ALIASES.get(str(fields["observation_type"]).strip().lower(), "")
            if not obs_type:
                errors.append({"code": "UNKNOWN_OBSERVATION_TYPE",
                               "message": f"未知观测类型: {fields['observation_type']!r}"})
            else:
                spec = OBSERVATION_TYPES[obs_type]

        unit_text = str(fields["unit"]).strip() if fields["unit"] not in (None, "") else ""
        factor: float | None = None
        canonical_unit = spec["canonical_unit"] if spec else ""
        converted = 0
        if spec:
            if not unit_text:
                errors.append({"code": "MISSING_FIELD", "message": "缺少单位"})
            else:
                factor = spec["units"].get(unit_text.lower())
                if factor is None:
                    errors.append({"code": "UNSUPPORTED_UNIT",
                                   "message": f"观测类型 {obs_type} 不支持单位 {unit_text!r}，允许: {', '.join(spec['units'])}"})
                else:
                    canonical_unit = spec["canonical_unit"]
                    converted = 0 if factor == 1.0 else 1

        value_canonical: float | None = None
        source_value: float | None = None
        if fields["value"] not in (None, ""):
            try:
                source_value = self._to_float(fields["value"])
            except (ValueError, TypeError):
                errors.append({"code": "INVALID_NUMBER", "message": f"数值无法解析: {fields['value']!r}"})
            else:
                if spec and factor is not None:
                    value_canonical = source_value * factor
                    if not (spec["min"] <= value_canonical <= spec["max"]):
                        errors.append({"code": "VALUE_OUT_OF_RANGE",
                                       "message": f"换算后 {value_canonical:g} {canonical_unit} 超出合理范围 "
                                                  f"[{spec['min']:g}, {spec['max']:g}]"})

        dl_canonical: float | None = 0.0
        err_canonical: float | None = 0.05
        if fields["detection_limit"] not in (None, ""):
            try:
                dl_source = self._to_float(fields["detection_limit"])
                dl_canonical = dl_source * (factor or 0.0) if (dl_source is not None and factor is not None) else dl_source
            except (ValueError, TypeError):
                dl_canonical = None
                errors.append({"code": "INVALID_NUMBER", "message": f"检测限无法解析: {fields['detection_limit']!r}"})
        if fields["measurement_error"] not in (None, ""):
            try:
                err_source = self._to_float(fields["measurement_error"])
                err_canonical = err_source * (factor or 0.0) if (err_source is not None and factor is not None) else err_source
            except (ValueError, TypeError):
                err_canonical = None
                errors.append({"code": "INVALID_NUMBER", "message": f"测量误差无法解析: {fields['measurement_error']!r}"})
        if dl_canonical is not None and not (0 <= dl_canonical <= DETECTION_LIMIT_MAX):
            errors.append({"code": "VALUE_OUT_OF_RANGE", "message": "检测限超出允许范围"})
        if err_canonical is not None and not (0 <= err_canonical <= MEASUREMENT_ERROR_MAX):
            errors.append({"code": "VALUE_OUT_OF_RANGE", "message": "测量误差超出允许范围"})

        # 检测限/误差列错位的常见征兆：误差反而比检测限大很多，或检测限大于溶质数值两个数量级以上
        if dl_canonical and err_canonical and err_canonical > dl_canonical * 10:
            errors.append({"code": "SUSPECTED_MISALIGNED_COLUMN",
                           "message": "测量误差显著大于检测限，检测限/误差列可能错位"})

        return {
            "errors": errors, "well_id": well_id, "well_code": well_code,
            "sample_code": sample_code, "sampled_at": sampled_at,
            "observation_type": obs_type, "source_unit": unit_text,
            "source_value": source_value, "converted": converted,
            "canonical_unit": canonical_unit, "value_canonical": value_canonical,
            "detection_limit_canonical": dl_canonical, "measurement_error_canonical": err_canonical,
        }

    @staticmethod
    def _same_number(left: float | None, right: float | None) -> bool:
        if left is None or right is None:
            return False
        return abs(left - right) <= 1e-9 + 1e-9 * abs(right)

    def _recompute_duplicates(self, connection: sqlite3.Connection, batch_id: int) -> None:
        """对未人工裁定的暂存行重新做批内与库内查重；坏行保持 rejected。"""
        connection.execute(
            "UPDATE hydro_import_rows SET status='staged', error_code='', error_json='[]' "
            "WHERE batch_id=? AND status IN ('staged','suspected_duplicate') AND duplicate_resolution=''",
            (batch_id,))
        rows = connection.execute(
            "SELECT * FROM hydro_import_rows WHERE batch_id=? AND status='staged' AND duplicate_resolution='' "
            "ORDER BY row_number", (batch_id,)).fetchall()
        seen_keys: set[tuple] = set()
        for row in rows:
            reasons: list[str] = []
            pair = (row["sample_code"], row["observation_type"])
            triple = (row["well_code"], row["sampled_at"], row["observation_type"])
            if pair in seen_keys:
                reasons.append("批内重复：同样本编号下同一观测类型出现多次")
            seen_keys.add(pair)
            existing = connection.execute(
                "SELECT hs.sample_code, hs.sampled_at, hs.well_id, hw.code AS well_code, "
                "hs.isotope_d18o, hs.isotope_d2h, hs.solute_mg_l "
                "FROM hydro_samples hs LEFT JOIN hydro_wells hw ON hw.id=hs.well_id "
                "WHERE hs.sample_code=?", (row["sample_code"],)).fetchall()
            if existing:
                column = OBSERVATION_TYPES[row["observation_type"]]["column"]
                same_well = next((e for e in existing if e["well_code"] == row["well_code"]), None)
                match = next((e for e in existing if self._same_number(row["value_canonical"], e[column])), None)
                if same_well is not None and self._same_number(row["value_canonical"], same_well[column]):
                    reasons.append("样本编号已存在且观测值一致")
                elif match is not None:
                    reasons.append(f"样本编号在井点 {match['well_code']} 已存在且观测值一致，疑似重复提交")
                else:
                    reasons.append("样本编号已存在但观测值不同，需人工核对")
            else:
                twin = connection.execute(
                    "SELECT hs.id FROM hydro_samples hs JOIN hydro_wells hw ON hw.id=hs.well_id "
                    "WHERE hw.code=? AND hs.sampled_at=?", (row["well_code"], row["sampled_at"])).fetchall()
                for other in twin:
                    other_row = connection.execute(
                        "SELECT * FROM hydro_samples WHERE id=?", (other["id"],)).fetchone()
                    column = OBSERVATION_TYPES[row["observation_type"]]["column"]
                    if self._same_number(row["value_canonical"], other_row[column]):
                        reasons.append(f"疑似重复观测：同井同时刻同类型同值（样本 {other_row['sample_code']}）")
                        break
            if reasons:
                connection.execute(
                    "UPDATE hydro_import_rows SET status='suspected_duplicate', error_code='SUSPECTED_DUPLICATE', "
                    "error_json=? WHERE id=?", (json.dumps(
                        [{"code": "SUSPECTED_DUPLICATE", "message": reason} for reason in reasons],
                        ensure_ascii=False), row["id"]))

    def _refresh_summary(self, connection: sqlite3.Connection, batch_id: int) -> dict[str, Any]:
        counts = {
            "total": connection.execute("SELECT COUNT(*) c FROM hydro_import_rows WHERE batch_id=?", (batch_id,)).fetchone()["c"],
            "accepted": connection.execute("SELECT COUNT(*) c FROM hydro_import_rows WHERE batch_id=? AND status='staged'", (batch_id,)).fetchone()["c"],
            "rejected": connection.execute("SELECT COUNT(*) c FROM hydro_import_rows WHERE batch_id=? AND status='rejected'", (batch_id,)).fetchone()["c"],
            "suspected_duplicates": connection.execute("SELECT COUNT(*) c FROM hydro_import_rows WHERE batch_id=? AND status='suspected_duplicate'", (batch_id,)).fetchone()["c"],
            "converted": connection.execute("SELECT COUNT(*) c FROM hydro_import_rows WHERE batch_id=? AND converted=1 AND status!='rejected'", (batch_id,)).fetchone()["c"],
            "confirmed_rows": connection.execute("SELECT COUNT(*) c FROM hydro_import_rows WHERE batch_id=? AND status='confirmed'", (batch_id,)).fetchone()["c"],
        }
        groups = connection.execute(
            "SELECT COUNT(DISTINCT well_code||char(31)||sample_code||char(31)||sampled_at) c "
            "FROM hydro_import_rows WHERE batch_id=? AND status='staged'", (batch_id,)).fetchone()["c"]
        counts["ready_groups"] = groups
        connection.execute("UPDATE hydro_import_batches SET summary_json=? WHERE id=?",
                           (json.dumps(counts, ensure_ascii=False), batch_id))
        return counts

    def _serialize_batch(self, connection: sqlite3.Connection, batch: sqlite3.Row, *, replayed: bool = False) -> dict[str, Any]:
        rows = []
        for row in connection.execute("SELECT * FROM hydro_import_rows WHERE batch_id=? ORDER BY row_number", (batch["id"],)).fetchall():
            item = dict(row)
            item["errors"] = json.loads(row["error_json"] or "[]")
            item["raw"] = json.loads(row["raw_json"])
            item["payload"] = json.loads(row["payload_json"])
            for key in ("error_json", "raw_json", "payload_json"):
                item.pop(key)
            rows.append(item)
        return {
            "id": batch["id"], "source_batch_id": batch["source_batch_id"],
            "status": batch["status"], "replayed": replayed,
            "created_at": batch["created_at"], "confirmed_at": batch["confirmed_at"],
            "summary": json.loads(batch["summary_json"] or "{}"), "rows": rows,
        }

    def create_import(self, payload: dict[str, Any], actor: str = "researcher") -> dict[str, Any]:
        raw_rows = self._parse_import_payload(payload)
        request_hash = _digest([{k: v for k, v in row.items() if k != "_row_number"} for row in raw_rows])
        with transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT * FROM hydro_import_batches WHERE source_batch_id=?",
                (payload["source_batch_id"],)).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise ValueError("import_batch_id_conflict")
                return self._serialize_batch(connection, existing, replayed=True)

            now = _now()
            cursor = connection.execute(
                "INSERT INTO hydro_import_batches(source_batch_id,request_hash,created_at) VALUES(?,?,?)",
                (payload["source_batch_id"], request_hash, now))
            batch_id = cursor.lastrowid
            for raw in raw_rows:
                result = self._validate_row(connection, raw)
                row_number = raw["_row_number"]
                snapshot = json.dumps({k: v for k, v in raw.items() if k != "_row_number"}, ensure_ascii=False)
                effective = json.dumps({
                    "well_code": result["well_code"], "sample_code": result["sample_code"],
                    "sampled_at": result["sampled_at"], "observation_type": result["observation_type"],
                    "canonical_unit": result["canonical_unit"], "value_canonical": result["value_canonical"],
                    "detection_limit_canonical": result["detection_limit_canonical"],
                    "measurement_error_canonical": result["measurement_error_canonical"],
                }, ensure_ascii=False)
                status = "rejected" if result["errors"] else "staged"
                error_json = json.dumps(result["errors"], ensure_ascii=False)
                connection.execute(
                    "INSERT INTO hydro_import_rows(batch_id,row_number,status,error_code,error_json,"
                    "well_code,sample_code,sampled_at,observation_type,source_unit,source_value,"
                    "converted,canonical_unit,value_canonical,detection_limit_canonical,"
                    "measurement_error_canonical,raw_json,payload_json) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (batch_id, row_number, status, result["errors"][0]["code"] if result["errors"] else "",
                     error_json, result["well_code"], result["sample_code"], result["sampled_at"],
                     result["observation_type"], result["source_unit"], result["source_value"],
                     result["converted"], result["canonical_unit"], result["value_canonical"],
                     result["detection_limit_canonical"], result["measurement_error_canonical"],
                     snapshot, effective))
            self._recompute_duplicates(connection, batch_id)
            summary = self._refresh_summary(connection, batch_id)
            connection.execute(
                "INSERT INTO hydro_audit(resource_type,resource_id,action,actor,payload_json,created_at) "
                "VALUES('import_batch',?,'stage',?,?,?)",
                (batch_id, actor, json.dumps({"source_batch_id": payload["source_batch_id"], "summary": summary},
                                             ensure_ascii=False), now))
            batch = connection.execute("SELECT * FROM hydro_import_batches WHERE id=?", (batch_id,)).fetchone()
            return self._serialize_batch(connection, batch)

    def get_import(self, batch_id: int) -> dict[str, Any] | None:
        batch = self.connection.execute("SELECT * FROM hydro_import_batches WHERE id=?", (batch_id,)).fetchone()
        return None if batch is None else self._serialize_batch(self.connection, batch)

    def fix_import_rows(self, batch_id: int, updates: list[dict[str, Any]], actor: str = "researcher") -> dict[str, Any]:
        editable = {"well_code", "sample_code", "sampled_at", "observation_type", "unit",
                    "value", "detection_limit", "measurement_error"}
        with transaction(immediate=True) as connection:
            batch = connection.execute("SELECT * FROM hydro_import_batches WHERE id=?", (batch_id,)).fetchone()
            if batch is None:
                raise KeyError("import_batch_not_found")
            for update in updates:
                row_number = update.get("row_number")
                if not isinstance(row_number, int):
                    raise ValueError("row_number_required")
                stored = connection.execute(
                    "SELECT * FROM hydro_import_rows WHERE batch_id=? AND row_number=?",
                    (batch_id, row_number)).fetchone()
                if stored is None:
                    raise KeyError(f"row_not_found:{row_number}")
                if stored["status"] == "confirmed":
                    raise ValueError(f"row_already_confirmed:{row_number}")
                merged = json.loads(stored["raw_json"])
                for key, value in update.items():
                    if key != "row_number" and key in editable:
                        merged[key] = value
                result = self._validate_row(connection, merged)
                error_json = json.dumps(result["errors"], ensure_ascii=False)
                effective = json.dumps({
                    "well_code": result["well_code"], "sample_code": result["sample_code"],
                    "sampled_at": result["sampled_at"], "observation_type": result["observation_type"],
                    "canonical_unit": result["canonical_unit"], "value_canonical": result["value_canonical"],
                    "detection_limit_canonical": result["detection_limit_canonical"],
                    "measurement_error_canonical": result["measurement_error_canonical"],
                }, ensure_ascii=False)
                connection.execute(
                    "UPDATE hydro_import_rows SET status=?,error_code=?,error_json=?,"
                    "well_code=?,sample_code=?,sampled_at=?,observation_type=?,source_unit=?,"
                    "source_value=?,converted=?,canonical_unit=?,value_canonical=?,"
                    "detection_limit_canonical=?,measurement_error_canonical=?,payload_json=?,"
                    "duplicate_resolution='',sample_id=NULL WHERE id=?",
                    ("rejected" if result["errors"] else "staged",
                     result["errors"][0]["code"] if result["errors"] else "", error_json,
                     result["well_code"], result["sample_code"], result["sampled_at"],
                     result["observation_type"], result["source_unit"], result["source_value"],
                     result["converted"], result["canonical_unit"], result["value_canonical"],
                     result["detection_limit_canonical"], result["measurement_error_canonical"],
                     effective, stored["id"]))
            self._recompute_duplicates(connection, batch_id)
            self._refresh_summary(connection, batch_id)
            connection.execute(
                "INSERT INTO hydro_audit(resource_type,resource_id,action,actor,payload_json,created_at) "
                "VALUES('import_batch',?,'fix',?,?,?)",
                (batch_id, actor, json.dumps({"rows": [u.get("row_number") for u in updates]}, ensure_ascii=False), _now()))
            batch = connection.execute("SELECT * FROM hydro_import_batches WHERE id=?", (batch_id,)).fetchone()
            return self._serialize_batch(connection, batch)

    def _confirmable_clause(self) -> str:
        # 可入库：暂存有效行，或人工裁定接受的疑似重复行
        return "(status='staged' OR (status='suspected_duplicate' AND duplicate_resolution='accepted'))"

    def resolve_duplicate_rows(self, batch_id: int, resolutions: list[dict[str, Any]], actor: str = "researcher") -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            batch = connection.execute("SELECT * FROM hydro_import_batches WHERE id=?", (batch_id,)).fetchone()
            if batch is None:
                raise KeyError("import_batch_not_found")
            for item in resolutions:
                row_number = item.get("row_number")
                decision = item.get("resolution")
                if not isinstance(row_number, int) or decision not in ("accepted", "rejected"):
                    raise ValueError("invalid_resolution")
                stored = connection.execute(
                    "SELECT * FROM hydro_import_rows WHERE batch_id=? AND row_number=?",
                    (batch_id, row_number)).fetchone()
                if stored is None:
                    raise KeyError(f"row_not_found:{row_number}")
                if stored["status"] != "suspected_duplicate":
                    raise ValueError(f"row_not_duplicate:{row_number}")
                if decision == "accepted":
                    connection.execute(
                        "UPDATE hydro_import_rows SET duplicate_resolution='accepted', status='staged' WHERE id=?",
                        (stored["id"],))
                else:
                    # 人工驳回：该行按拒绝处理，但保留重复原因，且不再阻塞同组确认
                    connection.execute(
                        "UPDATE hydro_import_rows SET duplicate_resolution='rejected', status='rejected', "
                        "error_code='DUPLICATE_REJECTED' WHERE id=?", (stored["id"],))
            self._refresh_summary(connection, batch_id)
            connection.execute(
                "INSERT INTO hydro_audit(resource_type,resource_id,action,actor,payload_json,created_at) "
                "VALUES('import_batch',?,'resolve_duplicates',?,?,?)",
                (batch_id, actor, json.dumps(resolutions, ensure_ascii=False), _now()))
            batch = connection.execute("SELECT * FROM hydro_import_batches WHERE id=?", (batch_id,)).fetchone()
            return self._serialize_batch(connection, batch)

    def confirm_import(self, batch_id: int, row_numbers: list[int] | None = None, actor: str = "researcher") -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            batch = connection.execute("SELECT * FROM hydro_import_batches WHERE id=?", (batch_id,)).fetchone()
            if batch is None:
                raise KeyError("import_batch_not_found")
            if row_numbers:
                wanted = connection.execute(
                    f"SELECT * FROM hydro_import_rows WHERE batch_id=? AND row_number IN ("
                    + ",".join("?" for _ in row_numbers) + ")", (batch_id, *row_numbers)).fetchall()
                missing = sorted(set(row_numbers) - {r["row_number"] for r in wanted})
                if missing:
                    raise KeyError(f"row_not_found:{missing[0]}")
                # 扩展到这些行所属样本组的全部行（含坏行/未裁定重复行）
                group_keys = {(r["well_code"], r["sample_code"], r["sampled_at"]) for r in wanted}
                group_rows = []
                for key in group_keys:
                    group_rows.extend(connection.execute(
                        "SELECT * FROM hydro_import_rows WHERE batch_id=? AND well_code=? AND sample_code=? "
                        "AND sampled_at=? ORDER BY row_number", (batch_id, *key)).fetchall())
                blocked_here = {
                    r["row_number"]: [r["row_number"]]
                    for r in group_rows
                    if r["status"] not in ("staged", "confirmed") and r["error_code"] != "DUPLICATE_REJECTED"
                }
                if blocked_here:
                    connection.execute(
                        "INSERT INTO hydro_audit(resource_type,resource_id,action,actor,payload_json,created_at) "
                        "VALUES('import_batch',?,'confirm_blocked',?,?,?)",
                        (batch_id, actor, json.dumps({"blocked": blocked_here}, ensure_ascii=False), _now()))
                    raise ValidationError(
                        "指定的样本组仍含坏行或未裁定的疑似重复行，请修正或裁定后再确认",
                        context={"blocked_rows": blocked_here},
                    )
                selected = [r for r in group_rows if r["status"] == "staged"]
                selected.sort(key=lambda r: r["row_number"])
            else:
                selected = connection.execute(
                    f"SELECT * FROM hydro_import_rows WHERE batch_id=? AND {self._confirmable_clause()} "
                    "ORDER BY row_number", (batch_id,)).fetchall()
            if not selected:
                raise ValueError("no_staged_rows_to_confirm")
            # 同一样本组内只要还有坏行/未裁定的疑似重复行，就整组暂缓，避免半截样本污染数据
            blocked: dict[int, list[int]] = {}
            chosen: list[sqlite3.Row] = []
            for row in selected:
                siblings = connection.execute(
                    "SELECT row_number,status FROM hydro_import_rows WHERE batch_id=? "
                    "AND well_code=? AND sample_code=? AND sampled_at=? AND status!='confirmed' "
                    "AND error_code!='DUPLICATE_REJECTED'",
                    (batch_id, row["well_code"], row["sample_code"], row["sampled_at"])).fetchall()
                bad = [s["row_number"] for s in siblings if s["status"] != "staged"]
                if bad:
                    blocked[row["row_number"]] = sorted(set(bad))
                else:
                    chosen.append(row)
            if not chosen:
                raise ValidationError(
                    "没有可确认的干净样本组（其他组含坏行或未裁定重复行）",
                    context={"blocked_rows": blocked},
                )
            # 去重（按行号扩展组时可能重复选中）
            unique_chosen = {row["id"]: row for row in chosen}
            chosen = sorted(unique_chosen.values(), key=lambda r: r["row_number"])

            groups: dict[tuple, list[sqlite3.Row]] = {}
            for row in chosen:
                groups.setdefault((row["well_code"], row["sample_code"], row["sampled_at"]), []).append(row)

            samples_inserted = 0
            now = _now()
            for (well_code, sample_code, sampled_at), members in groups.items():
                well_id = connection.execute("SELECT id FROM hydro_wells WHERE code=?", (well_code,)).fetchone()["id"]
                values: dict[str, float | None] = {"isotope_d18o": None, "isotope_d2h": None, "solute_mg_l": None}
                for member in members:
                    values[OBSERVATION_TYPES[member["observation_type"]]["column"]] = member["value_canonical"]
                detection_limit = max(member["detection_limit_canonical"] or 0.0 for member in members)
                measurement_error = max(member["measurement_error_canonical"] or 0.0 for member in members)
                quality = "usable" if sum(v is not None for v in values.values()) >= 2 else "incomplete"
                try:
                    cursor = connection.execute(
                        "INSERT INTO hydro_samples(well_id,sample_code,sampled_at,isotope_d18o,isotope_d2h,"
                        "solute_mg_l,detection_limit,measurement_error,quality_status,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (well_id, sample_code, sampled_at, values["isotope_d18o"], values["isotope_d2h"],
                         values["solute_mg_l"], detection_limit, measurement_error, quality, now))
                except sqlite3.IntegrityError as exc:
                    raise ValueError(f"sample_code_conflict:{sample_code}") from exc
                sample_id = cursor.lastrowid
                samples_inserted += 1
                connection.execute(
                    "UPDATE hydro_import_rows SET status='confirmed',sample_id=? WHERE id IN ("
                    + ",".join("?" for _ in members) + ")", (sample_id, *[m["id"] for m in members]))

            remaining = connection.execute(
                "SELECT COUNT(*) c FROM hydro_import_rows WHERE batch_id=? AND status!='confirmed'",
                (batch_id,)).fetchone()["c"]
            status = "confirmed" if remaining == 0 else "partially_confirmed"
            connection.execute("UPDATE hydro_import_batches SET status=?,confirmed_at=? WHERE id=?",
                               (status, now if remaining == 0 else None, batch_id))
            summary = self._refresh_summary(connection, batch_id)
            summary["samples_inserted_this_time"] = samples_inserted
            connection.execute("UPDATE hydro_import_batches SET summary_json=? WHERE id=?",
                               (json.dumps(summary, ensure_ascii=False), batch_id))
            connection.execute(
                "INSERT INTO hydro_audit(resource_type,resource_id,action,actor,payload_json,created_at) "
                "VALUES('import_batch',?,'confirm',?,?,?)",
                (batch_id, actor, json.dumps({"samples_inserted": samples_inserted}, ensure_ascii=False), now))
            batch = connection.execute("SELECT * FROM hydro_import_batches WHERE id=?", (batch_id,)).fetchone()
            return self._serialize_batch(connection, batch)
