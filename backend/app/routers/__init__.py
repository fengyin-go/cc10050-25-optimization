"""业务路由汇总：恢复重排等专用路由先注册，通用资源动态路由兜底。"""
from __future__ import annotations

from app.routers import recovery, resource

ROUTERS = [recovery, resource]
