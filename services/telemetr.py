"""
Сервис для работы с Telemetr API
"""
import logging
from typing import Optional
import aiohttp

from config import TELEMETR_API_TOKEN, TELEMETR_API_URL


logger = logging.getLogger(__name__)


class TelemetrService:
    """Сервис для получения аналитики каналов через Telemetr API"""
    
    def __init__(self, api_token: str = TELEMETR_API_TOKEN):
        self.api_token = api_token
        self.base_url = TELEMETR_API_URL
    
    async def _request(self, endpoint: str, params: dict = None) -> Optional[dict]:
        """Выполнить запрос к API"""
        if not self.api_token:
            logger.warning("Telemetr API token not configured")
            return None
        
        try:
            headers = {
                "x-api-key": self.api_token,
                "accept": "application/json"
            }
            
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    f"{self.base_url}{endpoint}",
                    headers=headers,
                    params=params
                ) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    elif resp.status == 426:
                        logger.warning("Telemetr API quota reached")
                    else:
                        logger.error(f"Telemetr API error: {resp.status}")
                    return None
        except Exception as e:
            logger.error(f"Telemetr API request failed: {e}")
            return None
    
    async def resolve_telegram_id(self, telegram_id: int) -> Optional[str]:
        """Конвертировать Telegram ID в internal_id Telemetr"""
        clean_id = abs(telegram_id)
        if clean_id > 1000000000000:
            clean_id = clean_id - 1000000000000
        
        data = await self._request("/v1/utils/resolve_telegram_id", {"telegram_id": clean_id})
        if data and "internal_id" in data:
            return data["internal_id"]
        return None
    
    async def search_channel(self, username: str) -> Optional[dict]:
        """Найти канал по username"""
        data = await self._request("/v1/channels/search", {"term": username.lstrip("@"), "limit": 1})
        if data and isinstance(data, list) and len(data) > 0:
            return data[0]
        return None
    
    async def get_channel_stats(self, internal_id: str) -> Optional[dict]:
        """Получить статистику канала по internal_id"""
        data = await self._request("/v1/channel/stats", {"internal_id": internal_id})
        if data and isinstance(data, list) and len(data) > 0:
            return data[0]
        return data
    
    async def get_full_stats(self, telegram_id: int = None, username: str = None) -> Optional[dict]:
        """
        Получить полную статистику канала.
        
        Возвращает:
        {
            "internal_id": "xxx",
            "subscribers": 6384,
            "avg_views_24h": 527,
            "avg_views_48h": 638,
            "err_percent": 8.26,
            "err24_percent": 8.26,
            "title": "Название канала"
        }
        """
        internal_id = None
        
        if telegram_id:
            internal_id = await self.resolve_telegram_id(telegram_id)
        
        if not internal_id and username:
            channel = await self.search_channel(username)
            if channel:
                internal_id = channel.get("internal_id")
        
        if not internal_id:
            logger.warning(f"Could not find channel in Telemetr: tg_id={telegram_id}, username={username}")
            return None
        
        stats = await self.get_channel_stats(internal_id)
        if not stats:
            return None
        
        avg_post_views = stats.get("avg_post_views", {})
        
        return {
            "internal_id": internal_id,
            "title": stats.get("title", ""),
            "subscribers": stats.get("members_count", 0),
            "avg_views": avg_post_views.get("avg_post_views", 0),
            "avg_views_24h": avg_post_views.get("avg_post_views_24h", 0),
            "avg_views_48h": avg_post_views.get("avg_post_views_48h", 0),
            "err_percent": stats.get("err_percent", 0),
            "err24_percent": stats.get("err24_percent", 0),
        }


# Глобальный экземпляр сервиса
telemetr_service = TelemetrService()
