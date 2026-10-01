"""Top-level routers.

``api_v1_router`` is mounted at ``/api/v1``. Feature routers (auth, connections,
metadata, analytics, history, feedback, admin) are included here as each phase
lands; see docs/architecture.md "Delivery phases".
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api import (
    admin,
    analytics,
    analytics_ws,
    auth,
    connections,
    exports,
    feedback,
    health,
    history,
    metadata,
    semantic,
)

API_V1_PREFIX = "/api/v1"

# Operator endpoints: /health/live, /health/ready, /metrics
ops_router = APIRouter()
ops_router.include_router(health.router)

api_v1_router = APIRouter(prefix=API_V1_PREFIX)
api_v1_router.include_router(auth.router)
api_v1_router.include_router(connections.router)
api_v1_router.include_router(metadata.router)
api_v1_router.include_router(semantic.router)
api_v1_router.include_router(analytics.router)
api_v1_router.include_router(analytics_ws.router)
api_v1_router.include_router(history.router)
api_v1_router.include_router(feedback.router)
api_v1_router.include_router(exports.router)
api_v1_router.include_router(admin.router)
