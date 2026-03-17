"""
AI Тренер для менеджеров (Claude API)
"""
import logging
import re
import asyncio
from typing import Optional, Dict, List
from datetime import datetime, timedelta

import aiohttp
from sqlalchemy import select, func

from config import CLAUDE_API_KEY, CLAUDE_MODEL, AI_TRAINER_SYSTEM_PROMPT
from database import async_session_maker, AIInsight
from utils.helpers import utc_now


logger = logging.getLogger(__name__)

# Максимальный возраст истории диалога (неактивные сессии очищаются автоматически)
_CONVERSATION_TTL_HOURS = 24


class AITrainerService:
    """Сервис AI-тренера для менеджеров"""
    
    def __init__(self, api_key: str = CLAUDE_API_KEY):
        self.api_key = api_key
        self.base_url = "https://api.anthropic.com/v1/messages"
        # Структура: {user_id: {"history": [...], "last_active": datetime}}
        self.conversation_history: Dict[int, dict] = {}
    
    def _get_user_history(self, user_id: int) -> List[dict]:
        """Получить историю диалога пользователя, создав запись при необходимости."""
        return self.conversation_history.setdefault(
            user_id, {"history": [], "last_active": utc_now()}
        )["history"]

    def _touch_user_history(self, user_id: int) -> None:
        """Обновить метку активности пользователя."""
        if user_id in self.conversation_history:
            self.conversation_history[user_id]["last_active"] = utc_now()

    def _cleanup_stale_histories(self) -> None:
        """Удалить истории диалогов неактивных пользователей (старше TTL)."""
        cutoff = utc_now() - timedelta(hours=_CONVERSATION_TTL_HOURS)
        stale = []
        for uid, data in self.conversation_history.items():
            last_active = data.get("last_active")
            if last_active is None:
                logger.warning(f"AI Trainer: отсутствует last_active для user_id={uid}, запись будет очищена")
                stale.append(uid)
            elif last_active < cutoff:
                stale.append(uid)
        for uid in stale:
            del self.conversation_history[uid]
        if stale:
            logger.info(f"AI Trainer: очищена история {len(stale)} неактивных пользователей")
    
    async def get_response(
        self, 
        user_id: int, 
        user_message: str, 
        manager_name: str = "Менеджер"
    ) -> Optional[str]:
        """Получить ответ от AI-тренера"""
        
        if not self.api_key:
            logger.warning("Claude API key not configured")
            return "⚠️ AI-тренер временно недоступен. Обратитесь к администратору."
        
        # Периодически очищаем устаревшие истории (без отдельного таймера)
        self._cleanup_stale_histories()

        # Получаем историю пользователя и обновляем метку активности
        history = self._get_user_history(user_id)
        self._touch_user_history(user_id)
        
        # Добавляем сообщение пользователя
        history.append({
            "role": "user",
            "content": user_message
        })
        
        # Ограничиваем историю последними 10 сообщениями
        if len(history) > 10:
            self.conversation_history[user_id]["history"] = history[-10:]
            history = self.conversation_history[user_id]["history"]
        
        # Получаем частые темы для контекста
        frequent_topics = await self.get_frequent_topics()
        context_addition = ""
        if frequent_topics:
            context_addition = f"\n\nЧАСТЫЕ ВОПРОСЫ МЕНЕДЖЕРОВ (учитывай в ответах):\n{frequent_topics}"
        
        try:
            headers = {
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            }
            
            payload = {
                "model": CLAUDE_MODEL,
                "max_tokens": 512,
                "system": AI_TRAINER_SYSTEM_PROMPT + f"\n\nИмя менеджера: {manager_name}" + context_addition,
                "messages": history
            }
            
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    self.base_url,
                    headers=headers,
                    json=payload
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        assistant_message = data["content"][0]["text"]
                        
                        # Сохраняем ответ в историю
                        history.append({
                            "role": "assistant",
                            "content": assistant_message
                        })
                        
                        # Сохраняем инсайт для самообучения
                        await self.save_insight(user_id, user_message, assistant_message)
                        
                        # Убираем метку [TOPIC:...] из ответа
                        clean_response = self._remove_topic_tag(assistant_message)
                        
                        return clean_response
                    else:
                        error = await resp.text()
                        logger.error(f"Claude API error: {resp.status} - {error}")
                        return None
        except asyncio.TimeoutError:
            logger.error("Claude API timeout")
            return "⏱ Извини, ответ занял слишком много времени. Попробуй ещё раз."
        except Exception as e:
            logger.error(f"AI Trainer error: {e}")
            return None
    
    def _remove_topic_tag(self, text: str) -> str:
        """Убрать тег [TOPIC:...] из ответа"""
        return re.sub(r'\[TOPIC:\s*[^\]]+\]', '', text).strip()
    
    def _extract_topic(self, text: str) -> Optional[str]:
        """Извлечь тему из ответа"""
        match = re.search(r'\[TOPIC:\s*([^\]]+)\]', text)
        if match:
            return match.group(1).strip()
        return None
    
    async def save_insight(self, user_id: int, question: str, answer: str):
        """Сохранить инсайт для самообучения"""
        topic = self._extract_topic(answer)
        
        try:
            async with async_session_maker() as session:
                insight = AIInsight(
                    user_id=user_id,
                    topic=topic,
                    question=question,
                    answer=self._remove_topic_tag(answer)
                )
                session.add(insight)
                await session.commit()
        except Exception as e:
            logger.error(f"Error saving AI insight: {e}")
    
    async def save_feedback(self, user_id: int, feedback: str):
        """Сохранить фидбек на последний ответ"""
        try:
            async with async_session_maker() as session:
                result = await session.execute(
                    select(AIInsight)
                    .where(AIInsight.user_id == user_id)
                    .order_by(AIInsight.created_at.desc())
                    .limit(1)
                )
                insight = result.scalar_one_or_none()
                
                if insight:
                    insight.feedback = feedback
                    await session.commit()
        except Exception as e:
            logger.error(f"Error saving feedback: {e}")
    
    async def get_frequent_topics(self, limit: int = 5) -> str:
        """Получить частые темы вопросов"""
        try:
            async with async_session_maker() as session:
                result = await session.execute(
                    select(AIInsight.topic, func.count(AIInsight.id).label("cnt"))
                    .where(AIInsight.topic.isnot(None))
                    .group_by(AIInsight.topic)
                    .order_by(func.count(AIInsight.id).desc())
                    .limit(limit)
                )
                topics = result.all()
                
                if topics:
                    return "\n".join([f"- {t.topic} ({t.cnt} вопросов)" for t in topics])
                return ""
        except Exception as e:
            logger.error(f"Error getting frequent topics: {e}")
            return ""
    
    def clear_history(self, user_id: int):
        """Очистить историю диалога"""
        self.conversation_history.pop(user_id, None)

    async def get_post_recommendations(
        self,
        channel_name: str,
        views: int,
        reactions: int,
        forwards: int,
        saves: int,
        comments: int = 0,
        avg_channel_views: int = 0,
        cpm: float = 0,
    ) -> Optional[str]:
        """Получить AI-рекомендации по метрикам рекламного поста"""

        if not self.api_key:
            logger.warning("Claude API key not configured")
            return "⚠️ AI-анализ временно недоступен. Обратитесь к администратору."

        # Рассчитываем ER и другие производные метрики
        engagement = reactions + forwards + saves + comments
        er_percent = round(engagement / views * 100, 2) if views > 0 else 0
        reach_ratio = round(views / avg_channel_views * 100, 1) if avg_channel_views > 0 else None

        prompt_parts = [
            f"Канал: {channel_name}",
            f"Просмотры поста: {views}",
            f"Реакции: {reactions}",
            f"Пересылки: {forwards}",
            f"Сохранения: {saves}",
            f"Комментарии: {comments}",
            f"Engagement Rate: {er_percent}%",
        ]
        if reach_ratio is not None:
            prompt_parts.append(f"Охват vs средний охват канала: {reach_ratio}%")
        if cpm > 0:
            prompt_parts.append(f"CPM канала: {cpm:,.0f}₽")

        post_data = "\n".join(prompt_parts)

        system_prompt = (
            "Ты — эксперт по анализу рекламных постов в Telegram-каналах. "
            "На основе метрик поста дай краткие конкретные рекомендации:\n"
            "1. Оценка эффективности поста (хорошо/средне/плохо)\n"
            "2. Что работает и что не работает\n"
            "3. Как улучшить следующий пост (контент, время, формат)\n"
            "4. Стоит ли повторно размещать рекламу в этом канале\n"
            "Ответ до 600 символов. Используй эмодзи."
        )

        try:
            headers = {
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
            payload = {
                "model": CLAUDE_MODEL,
                "max_tokens": 512,
                "system": system_prompt,
                "messages": [{"role": "user", "content": post_data}],
            }
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(self.base_url, headers=headers, json=payload) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data["content"][0]["text"]
                    else:
                        error = await resp.text()
                        logger.error(f"Claude API error (post recommendations): {resp.status} - {error}")
                        return "⚠️ Ошибка API при получении рекомендации. Попробуйте позже."
        except asyncio.TimeoutError:
            logger.error("Claude API timeout (post recommendations)")
            return "⏱ Ответ занял слишком много времени. Попробуй ещё раз."
        except Exception as e:
            logger.error(f"AI post recommendations error: {e}")
            return "⚠️ Ошибка при получении AI-рекомендации. Попробуйте позже."


# Глобальный экземпляр сервиса
ai_trainer_service = AITrainerService()
