from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Response

from app.hydro.importer import HydroImportService
from app.hydro.schemas import EndmemberCreate, ImportBatchCreate, ImportCorrectRequest, InversionRequest, SampleCreate, TransportRequest, WellCreate
from app.hydro.service import HydroService

router=APIRouter(prefix="/api/hydro",tags=["地下水科学计算"])

def service()->HydroService: return HydroService()

def import_service()->HydroImportService: return HydroImportService()

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

@router.post("/imports",status_code=201)
def submit_import(payload:ImportBatchCreate,response:Response):
    body,status_code=import_service().submit_batch(payload.model_dump())
    response.status_code=status_code
    return body

@router.get("/imports")
def list_imports(limit:int=Query(50,ge=1,le=200)): return import_service().list_batches(limit)

@router.get("/imports/{batch_id}")
def get_import(batch_id:int):
    value=import_service().get_batch(batch_id)
    if value is None: raise HTTPException(404,"导入批次不存在")
    return value

@router.patch("/imports/{batch_id}/rows")
def correct_import_rows(batch_id:int,payload:ImportCorrectRequest):
    try: return import_service().correct_rows(batch_id,[c.model_dump() for c in payload.corrections])
    except KeyError as exc: raise HTTPException(404,"导入批次不存在") from exc

@router.post("/imports/{batch_id}/confirm")
def confirm_import(batch_id:int):
    try: return import_service().confirm_batch(batch_id)
    except KeyError as exc: raise HTTPException(404,"导入批次不存在") from exc

@router.get("/wells/{well_id}/observations")
def list_observations(well_id:int):
    try: return import_service().list_observations(well_id)
    except KeyError as exc: raise HTTPException(404,"井点不存在") from exc
