"""
Сервис геймификации для менеджеров
"""
import logging
from typing import List, Optional
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select, update

from config import MANAGER_LEVELS
from database import async_session_maker, Manager, Competition


logger = logging.getLogger(__name__)


class GamificationService:
    """Сервис геймификации"""
    
    async def add_experience(self, manager_id: int, xp: int, reason: str = "") -> dict:
        """
        Добавить опыт менеджеру.
        
        Возвращает:
        {
            "new_xp": 150,
            "level_up": True,
            "new_level": 2,
            "level_name": "Джуниор"
        }
        """
        async with async_session_maker() as session:
            manager = await session.get(Manager, manager_id)
            
            if not manager:
                return {"error": "Manager not found"}
            
            old_level = manager.level
            manager.experience_points += xp
            
            log_msg = f"Manager {manager_id} +{xp} XP"
            if reason:
                log_msg += f" ({reason})"
            logger.debug(log_msg)
            
            # Проверяем повышение уровня
            new_level = self._calculate_level(manager.experience_points)
            level_up = new_level > old_level
            
            if level_up:
                manager.level = new_level
                level_info = MANAGER_LEVELS.get(new_level, MANAGER_LEVELS[1])
                manager.commission_rate = Decimal(str(level_info["commission"]))
            
            await session.commit()
            
            result = {
                "new_xp": manager.experience_points,
                "level_up": level_up,
                "new_level": new_level,
            }
            
            if level_up:
                result["level_name"] = MANAGER_LEVELS.get(new_level, {}).get("name", "")
            
            return result
    
    def _calculate_level(self, xp: int) -> int:
        """Рассчитать уровень по опыту"""
        if xp >= 5000:
            return 5
        elif xp >= 2000:
            return 4
        elif xp >= 800:
            return 3
        elif xp >= 200:
            return 2
        else:
            return 1
    
    async def check_achievements(self, manager_id: int) -> List[str]:
        """Проверить достижения менеджера"""
        achievements = []
        
        async with async_session_maker() as session:
            manager = await session.get(Manager, manager_id)
            
            if not manager:
                return achievements
            
            # Первая продажа
            if manager.total_sales == 1:
                achievements.append("🎯 Первая продажа!")
            
            # Milestones
            milestones = [5, 10, 25, 50, 100]
            for m in milestones:
                if manager.total_sales == m:
                    achievements.append(f"🏆 {m} продаж!")
            
            # Выручка
            revenue_milestones = [10000, 50000, 100000, 500000]
            for r in revenue_milestones:
                if float(manager.total_revenue) >= r and float(manager.total_revenue) - float(manager.total_earned) < r:
                    achievements.append(f"💰 Выручка {r:,}₽!")
        
        return achievements
    
    async def get_leaderboard(self, metric: str = "sales", limit: int = 10) -> List[dict]:
        """Получить таблицу лидеров"""
        async with async_session_maker() as session:
            if metric == "sales":
                order_by = Manager.total_sales.desc()
            elif metric == "revenue":
                order_by = Manager.total_revenue.desc()
            elif metric == "xp":
                order_by = Manager.experience_points.desc()
            else:
                order_by = Manager.total_sales.desc()
            
            result = await session.execute(
                select(Manager)
                .where(Manager.is_active == True)
                .order_by(order_by)
                .limit(limit)
            )
            managers = result.scalars().all()
            
            leaderboard = []
            for i, m in enumerate(managers, 1):
                level_info = MANAGER_LEVELS.get(m.level, MANAGER_LEVELS[1])
                leaderboard.append({
                    "rank": i,
                    "name": m.first_name or m.username or "Менеджер",
                    "emoji": level_info["emoji"],
                    "sales": m.total_sales,
                    "revenue": float(m.total_revenue),
                    "xp": m.experience_points
                })
            
            return leaderboard
    
    async def create_monthly_competition(self) -> int:
        """Создать ежемесячное соревнование"""
        today = date.today()
        start = today.replace(day=1)
        
        # Последний день месяца
        if today.month == 12:
            end = today.replace(year=today.year + 1, month=1, day=1) - timedelta(days=1)
        else:
            end = today.replace(month=today.month + 1, day=1) - timedelta(days=1)
        
        async with async_session_maker() as session:
            competition = Competition(
                name=f"Лучший менеджер {start.strftime('%B %Y')}",
                description="Соревнование по количеству продаж",
                start_date=start,
                end_date=end,
                metric="sales",
                status="active"
            )
            session.add(competition)
            await session.commit()
            
            return competition.id
    
    async def process_sale(self, manager_id: int, order_amount: float) -> dict:
        """
        Обработать продажу: начислить XP и проверить достижения.
        
        Возвращает:
        {
            "xp_gained": 50,
            "level_up": False,
            "achievements": ["🎯 Первая продажа!"]
        }
        """
        # Рассчитываем XP
        xp = 10 + int(order_amount / 100)  # 10 базовых + 1 за каждые 100₽
        
        # Добавляем опыт
        xp_result = await self.add_experience(manager_id, xp, f"Продажа {order_amount}₽")
        
        # Проверяем достижения
        achievements = await self.check_achievements(manager_id)
        
        return {
            "xp_gained": xp,
            "level_up": xp_result.get("level_up", False),
            "new_level": xp_result.get("new_level"),
            "level_name": xp_result.get("level_name"),
            "achievements": achievements
        }


# Глобальный экземпляр сервиса
gamification_service = GamificationService()
