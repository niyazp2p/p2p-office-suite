from fastapi import APIRouter
from app.api.v1.endpoints import (
    auth,
    calendar,
    dashboard,
    financials,
    health,
    hr,
    tasks,
    timesheets,
    users,
    websockets,
)

api_router = APIRouter()

api_router.include_router(health.router, prefix="/health", tags=["Health"])
api_router.include_router(auth.router, prefix="/auth", tags=["Authentication"])
api_router.include_router(users.router, prefix="/users", tags=["Users & Hierarchy"])
api_router.include_router(tasks.router, prefix="/tasks", tags=["Task & Gantt Management"])
api_router.include_router(timesheets.router, prefix="/timesheets", tags=["Timesheets"])
api_router.include_router(calendar.router, prefix="/calendar", tags=["Calendar Suite"])
api_router.include_router(hr.router, prefix="/hr", tags=["HR & Performance Suite"])
api_router.include_router(financials.router, prefix="/financials", tags=["Financials & Audit"])
api_router.include_router(dashboard.router, prefix="/dashboards", tags=["Dashboards"])
api_router.include_router(websockets.router, tags=["Realtime WebSockets"])