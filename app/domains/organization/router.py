"""Organization resource routes."""

import uuid

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext, get_request_context
from app.db.session import get_db
from app.domains.organization import service
from app.domains.organization.schemas import (
    DepartmentCreate,
    DepartmentUpdate,
    HolidayCreate,
    HolidayUpdate,
    LocationCreate,
    LocationUpdate,
    ScheduleCreate,
    ScheduleUpdate,
    TeamCreate,
    TeamUpdate,
)

router = APIRouter(tags=["organization"])


@router.get("/teams")
async def get_teams(
    ctx: RequestContext = Depends(get_request_context), db: AsyncSession = Depends(get_db)
) -> dict:
    return {"items": await service.teams(db, ctx)}


@router.get("/teams/{team_id}")
async def get_team(
    team_id: uuid.UUID,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await service.get_team(db, ctx, team_id)


@router.post("/teams", status_code=201)
async def post_team(
    body: TeamCreate,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await service.create_team(db, ctx, body.model_dump())


@router.patch("/teams/{team_id}")
async def patch_team(
    team_id: uuid.UUID,
    body: TeamUpdate,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await service.update_team(db, ctx, team_id, body.model_dump(exclude_unset=True))


@router.delete("/teams/{team_id}", status_code=204)
async def remove_team(
    team_id: uuid.UUID,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await service.delete_team(db, ctx, team_id)
    return Response(status_code=204)


@router.get("/departments")
async def get_departments(
    ctx: RequestContext = Depends(get_request_context), db: AsyncSession = Depends(get_db)
) -> dict:
    return {"items": await service.departments(db, ctx)}


@router.get("/departments/tree")
async def get_department_tree(
    ctx: RequestContext = Depends(get_request_context), db: AsyncSession = Depends(get_db)
) -> dict:
    return {"items": await service.department_tree(db, ctx)}


@router.get("/departments/analytics")
async def get_department_analytics(
    ctx: RequestContext = Depends(get_request_context), db: AsyncSession = Depends(get_db)
) -> dict:
    return {"items": await service.department_analytics(db, ctx)}


@router.get("/departments/{department_id}")
async def get_department(
    department_id: uuid.UUID,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await service.get_resource(db, ctx, "departments", department_id, "tenant.read")


@router.get("/departments/{department_id}/profile")
async def get_department_profile(
    department_id: uuid.UUID,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await service.department_profile(db, ctx, department_id)


@router.get("/departments/{department_id}/employees")
async def get_department_employees(
    department_id: uuid.UUID,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await service.department_employees(db, ctx, department_id)


@router.post("/departments/{department_id}/employees/{employee_id}")
async def add_department_employee(
    department_id: uuid.UUID,
    employee_id: uuid.UUID,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await service.assign_department_employee(db, ctx, department_id, employee_id)


@router.delete("/departments/{department_id}/employees/{employee_id}", status_code=204)
async def remove_department_employee(
    department_id: uuid.UUID,
    employee_id: uuid.UUID,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await service.remove_department_employee(db, ctx, department_id, employee_id)
    return Response(status_code=204)


@router.post("/departments", status_code=201)
async def post_department(
    body: DepartmentCreate,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await service.create_department(db, ctx, body.model_dump())


@router.patch("/departments/{department_id}")
async def patch_department(
    department_id: uuid.UUID,
    body: DepartmentUpdate,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await service.update_department(db, ctx, department_id, body.model_dump(exclude_unset=True))


@router.delete("/departments/{department_id}", status_code=204)
async def remove_department(
    department_id: uuid.UUID,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await service.delete_department(db, ctx, department_id)
    return Response(status_code=204)


@router.get("/locations")
async def get_locations(
    ctx: RequestContext = Depends(get_request_context), db: AsyncSession = Depends(get_db)
) -> dict:
    return {"items": await service.locations(db, ctx)}


@router.get("/locations/{location_id}")
async def get_location(
    location_id: uuid.UUID,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await service.get_resource(db, ctx, "locations", location_id, "tenant.read")


@router.post("/locations", status_code=201)
async def post_location(
    body: LocationCreate,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await service.create_location(db, ctx, body.model_dump())


@router.patch("/locations/{location_id}")
async def patch_location(
    location_id: uuid.UUID,
    body: LocationUpdate,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await service.update_location(db, ctx, location_id, body.model_dump(exclude_unset=True))


@router.delete("/locations/{location_id}", status_code=204)
async def remove_location(
    location_id: uuid.UUID,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await service.delete_location(db, ctx, location_id)
    return Response(status_code=204)


@router.get("/work-schedules")
async def get_schedules(
    ctx: RequestContext = Depends(get_request_context), db: AsyncSession = Depends(get_db)
) -> dict:
    return {"items": await service.schedules(db, ctx)}


@router.get("/work-schedules/{schedule_id}")
async def get_schedule(
    schedule_id: uuid.UUID,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await service.get_resource(db, ctx, "work_schedules", schedule_id, "attendance.read")


@router.post("/work-schedules", status_code=201)
async def post_schedule(
    body: ScheduleCreate,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await service.create_schedule(db, ctx, body.model_dump())


@router.patch("/work-schedules/{schedule_id}")
async def patch_schedule(
    schedule_id: uuid.UUID,
    body: ScheduleUpdate,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await service.update_schedule(db, ctx, schedule_id, body.model_dump(exclude_unset=True))


@router.delete("/work-schedules/{schedule_id}", status_code=204)
async def remove_schedule(
    schedule_id: uuid.UUID,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await service.delete_schedule(db, ctx, schedule_id)
    return Response(status_code=204)


@router.get("/holidays")
async def get_holidays(
    ctx: RequestContext = Depends(get_request_context), db: AsyncSession = Depends(get_db)
) -> dict:
    return {"items": await service.holidays(db, ctx)}


@router.get("/holidays/{holiday_id}")
async def get_holiday(
    holiday_id: uuid.UUID,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await service.get_resource(db, ctx, "holidays", holiday_id, "attendance.read")


@router.post("/holidays", status_code=201)
async def post_holiday(
    body: HolidayCreate,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await service.create_holiday(db, ctx, body.model_dump())


@router.patch("/holidays/{holiday_id}")
async def patch_holiday(
    holiday_id: uuid.UUID,
    body: HolidayUpdate,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await service.update_holiday(db, ctx, holiday_id, body.model_dump(exclude_unset=True))


@router.delete("/holidays/{holiday_id}", status_code=204)
async def remove_holiday(
    holiday_id: uuid.UUID,
    ctx: RequestContext = Depends(get_request_context),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await service.delete_holiday(db, ctx, holiday_id)
    return Response(status_code=204)
