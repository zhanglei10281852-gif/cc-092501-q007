from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from app.hydro.schemas import (
    EndmemberCreate, ImportConfirmRequest, ImportCreate, ImportFixRequest,
    ImportResolveRequest, InversionRequest, SampleCreate, TransportRequest, WellCreate,
)
from app.hydro.service import HydroService

router=APIRouter(prefix="/api/hydro",tags=["地下水科学计算"])

def service()->HydroService: return HydroService()

@router.post("/wells",status_code=201)
def create_well(payload:WellCreate):
    try: return service().create_well(payload.model_dump())
    except Exception as exc:
        if "UNIQUE" in str(exc).upper(): raise HTTPException(409,"井点编码已存在") from exc
        raise

@router.get("/wells/{well_id}")
def get_well(well_id:int):
    value=service().get_well(well_id)
    if value is None: raise HTTPException(404,"井点不存在")
    return value

@router.delete("/wells/{well_id}")
def delete_well(well_id:int):
    try: service().delete_well(well_id); return {"message":"井点已删除"}
    except KeyError as exc: raise HTTPException(404,"井点不存在") from exc

@router.post("/endmembers",status_code=201)
def create_endmember(payload:EndmemberCreate): return service().create_endmember(payload.model_dump())

@router.post("/wells/{well_id}/samples",status_code=201)
def add_sample(well_id:int,payload:SampleCreate):
    try: return service().add_sample(well_id,payload.model_dump())
    except KeyError as exc: raise HTTPException(404,"井点不存在") from exc

@router.post("/samples/{sample_id}/inversions",status_code=202)
def enqueue_inversion(sample_id:int,payload:InversionRequest):
    try: return service().enqueue_inversion(sample_id,payload.model_dump())
    except KeyError as exc: raise HTTPException(404,"样本不存在") from exc
    except ValueError as exc: raise HTTPException(422,str(exc)) from exc

@router.post("/inversions/{task_id}/run")
def run_inversion(task_id:int,worker_id:str=Query(...,min_length=1)):
    try: return service().run_inversion(task_id,worker_id)
    except KeyError as exc: raise HTTPException(404,"任务不存在") from exc
    except ValueError as exc: raise HTTPException(422,str(exc)) from exc

@router.post("/wells/{well_id}/transport",status_code=201)
def run_transport(well_id:int,payload:TransportRequest):
    try: return service().run_transport(well_id,payload.model_dump())
    except KeyError as exc: raise HTTPException(404,"井点不存在") from exc


def _http_error(exc: Exception) -> HTTPException:
    message=str(exc).replace("'", "")
    if message.startswith("import_batch_not_found"):
        return HTTPException(404,"导入批次不存在")
    if message.startswith("row_not_found"):
        return HTTPException(404,f"暂存行不存在: {message.rsplit(':',1)[-1]}")
    mapping={
        "import_rows_required":"未提供任何导入行（rows 或 csv_content 至少一个）",
        "import_too_many_rows":"单次导入行数超过上限 1000",
        "import_batch_id_conflict":"同一来源批次号已提交过不同内容，请更换批次号",
        "no_staged_rows_to_confirm":"没有可确认的暂存行",
        "row_number_required":"修正项必须包含整数行号 row_number",
    }
    for key,text in mapping.items():
        if message.startswith(key): return HTTPException(422,text)
    if message.startswith("invalid_resolution"):
        return HTTPException(422,"裁定结果必须为 accepted 或 rejected，且需提供行号")
    if message.startswith("row_not_duplicate"):
        return HTTPException(409,f"该行不是疑似重复行: {message.rsplit(':',1)[-1]}")
    if message.startswith("row_already_confirmed"):
        return HTTPException(409,f"该行已确认入库，不可修改: {message.rsplit(':',1)[-1]}")
    if message.startswith("sample_code_conflict"):
        return HTTPException(409,f"样本编号已存在: {message.rsplit(':',1)[-1]}")
    return HTTPException(422,message)


@router.post("/imports",status_code=201)
def create_import(payload:ImportCreate):
    try:
        result=service().create_import(payload.model_dump())
        return Response(status_code=201,headers={"Cache-Control":"no-store"},
                        content=json.dumps(result,ensure_ascii=False),media_type="application/json")
    except ValueError as exc: raise _http_error(exc) from exc


@router.get("/imports/{batch_id}")
def get_import(batch_id:int):
    result=service().get_import(batch_id)
    if result is None: raise HTTPException(404,"导入批次不存在")
    return result


@router.post("/imports/{batch_id}/fix")
def fix_import(batch_id:int,payload:ImportFixRequest):
    updates=[u.model_dump(exclude_unset=True) for u in payload.updates]
    try: return service().fix_import_rows(batch_id,updates)
    except KeyError as exc: raise _http_error(exc) from exc
    except ValueError as exc: raise _http_error(exc) from exc


@router.post("/imports/{batch_id}/resolve-duplicates")
def resolve_import_duplicates(batch_id:int,payload:ImportResolveRequest):
    resolutions=[r.model_dump() for r in payload.resolutions]
    try: return service().resolve_duplicate_rows(batch_id,resolutions)
    except KeyError as exc: raise _http_error(exc) from exc
    except ValueError as exc: raise _http_error(exc) from exc


@router.post("/imports/{batch_id}/confirm",status_code=201)
def confirm_import(batch_id:int,payload:ImportConfirmRequest):
    try: return service().confirm_import(batch_id,payload.row_numbers)
    except KeyError as exc: raise _http_error(exc) from exc
    except ValueError as exc: raise _http_error(exc) from exc
