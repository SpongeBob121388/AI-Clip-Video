"""应用偏好持久化（非机密项）。

机密项（API Key、可能内嵌凭据的 base_url）走 CredentialStore；
这里只存可明文落库的偏好，写入在同一事务内完成，避免部分保存。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.persistence.database import get_global_engine
from app.persistence.models.global_models import AppSetting
from app.utils.logging import get_logger

logger = get_logger("persistence.preferences")

MAX_VALUE_LEN = 512

#: 允许写入的偏好键 —— 越界键一律拒绝，防止把机密写进明文表
PREFERENCE_KEYS: dict[str, Any] = {
    "asr_engine": str,
    "asr_model_size": str,
    "default_resolution": str,
    "default_aspect_ratio": str,
    "log_level": str,
}


class PreferenceError(ValueError):
    pass


def _validate(key: str, value: object) -> str:
    if key not in PREFERENCE_KEYS:
        raise PreferenceError(f"未知偏好键: {key}。允许: {sorted(PREFERENCE_KEYS)}")
    expected = PREFERENCE_KEYS[key]
    if not isinstance(value, expected):
        raise PreferenceError(f"偏好 {key} 类型应为 {expected.__name__}")
    text = str(value)
    if not text or len(text) > MAX_VALUE_LEN:
        raise PreferenceError(f"偏好 {key} 长度需在 1..{MAX_VALUE_LEN}")
    return text


async def set_preferences(values: dict[str, object]) -> list[str]:
    """在同一事务内 upsert 多个偏好；任一非法则整体不写。"""
    if not values:
        return []

    cleaned = {key: _validate(key, value) for key, value in values.items()}

    engine = await get_global_engine()
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as session:
        try:
            for key, value in cleaned.items():
                existing = await session.get(AppSetting, key)
                if existing is None:
                    session.add(AppSetting(key=key, value=value, category="preference"))
                else:
                    existing.value = value
            await session.commit()
        except Exception:
            await session.rollback()
            raise

    logger.info("preferences_saved", keys=sorted(cleaned))
    return sorted(cleaned)


async def get_preferences() -> dict[str, str]:
    engine = await get_global_engine()
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as session:
        result = await session.execute(
            select(AppSetting).where(AppSetting.key.in_(sorted(PREFERENCE_KEYS)))
        )
        return {row.key: row.value for row in result.scalars().all()}


async def get_preference(key: str, default: str | None = None) -> str | None:
    if key not in PREFERENCE_KEYS:
        raise PreferenceError(f"未知偏好键: {key}")
    prefs = await get_preferences()
    return prefs.get(key, default)
