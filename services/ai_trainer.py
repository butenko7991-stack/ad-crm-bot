"""
AI Тренер для менеджеров (Claude API)
"""
import logging
import re
import asyncio
from typing import Optional, Dict, List
from datetime import datetime

import aiohttp
from sqlalchemy import select, func

from config import CLAUDE_API_KEY, CLAUDE_MODEL, AI_TRAINER_SYSTEM_PROMPT
from database import async_session_maker, AIInsight


logger = logging.getLogger(__name__)


class AITrainerService:
    """Сервис AI-тренера для менеджеров"""
    
    def __init__(self, api_key: str = CLAUDE_API_KEY):
        self.api_key = api_key
        self.base_url = "https://api.anthropic.com/v1/messages"
        self.conversation_history: Dict[int, List[dict]] = {}
    
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
        
        # Инициализируем историю для пользователя
        if user_id not in self.conversation_history:
            self.conversation_history[user_id] = []
        
        # Добавляем сообщение пользователя
        self.conversation_history[user_id].append({
            "role": "user",
            "content": user_message
        })
        
        # Ограничиваем историю последними 10 сообщениями
        if len(self.conversation_history[user_id]) > 10:
            self.conversation_history[user_id] = self.conversation_history[user_id][-10:]
        
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
                "messages": self.conversation_history[user_id]
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
                        self.conversation_history[user_id].append({
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
        if user_id in self.conversation_history:
            del self.conversation_history[user_id]


# Глобальный экземпляр сервиса
ai_trainer_service = AITrainerService()
