"""
Handlers package
"""
from aiogram import Router

from handlers.common import router as common_router
from handlers.admin import router as admin_router
from handlers.manager import router as manager_router
from handlers.training import router as training_router
from handlers.client import router as client_router


def setup_routers() -> Router:
    """Настроить все роутеры"""
    main_router = Router()
    
    # Порядок важен! Сначала общие, потом специфичные
    main_router.include_router(common_router)
    main_router.include_router(admin_router)
    main_router.include_router(manager_router)
    main_router.include_router(training_router)
    main_router.include_router(client_router)
    
    return main_router

