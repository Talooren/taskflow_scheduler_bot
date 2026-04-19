from aiogram import Router

from app.handlers import common, reactions, moderator, executor

router = Router()
router.include_router(common.router)
router.include_router(reactions.router)
router.include_router(moderator.router)
router.include_router(executor.router)  # catch-all — последним

__all__ = ["router"]
