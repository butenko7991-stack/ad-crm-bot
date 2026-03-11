"""
Services package
"""
from services.telemetr import telemetr_service, TelemetrService
from services.ai_trainer import ai_trainer_service, AITrainerService
from services.gamification import gamification_service, GamificationService
from services.diagnostics import run_diagnostics, gather_business_metrics, get_improvement_suggestions

__all__ = [
    "telemetr_service", "TelemetrService",
    "ai_trainer_service", "AITrainerService", 
    "gamification_service", "GamificationService",
    "run_diagnostics", "gather_business_metrics", "get_improvement_suggestions",
]
