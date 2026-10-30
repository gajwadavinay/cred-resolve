"""
Tests for Predictive Pacing Engine.
"""
import pytest
from app.predictive_engine import compute_pacing_recommendation, PacingDecision


class TestPredictiveEngine:

    def test_recommendation_with_known_answer_rate(self, db):
        """Predictive: formula produces correct recommendation."""
        # 10 available, 5% failure rate, answer rate 0.5, 0 ringing
        decision = compute_pacing_recommendation(
            db=db,
            available_agents=10,
            reserved_agents=0,
            connected_calls=0,
            ringing_calls=0,
            provider_failure_rate=0.05,
            override_answer_rate=0.50,
            override_talk_time=120.0,
        )
        # effective_available = 10
        # max_ideal = floor(10 / 0.5) = 20
        # adjusted for 5% failure = ceil(20 / 0.95) = 22
        # ringing=0 → recommendation = 22
        assert decision.recommended_calls == 22
        assert not decision.fallback_to_progressive

    def test_fallback_when_answer_rate_too_low(self, db):
        """Predictive: falls back to progressive when answer rate < 5%."""
        decision = compute_pacing_recommendation(
            db=db,
            available_agents=10,
            reserved_agents=0,
            connected_calls=0,
            ringing_calls=0,
            override_answer_rate=0.02,  # below threshold
            override_talk_time=120.0,
        )
        assert decision.fallback_to_progressive
        # In fallback, recommended = effective_available (progressive)
        assert decision.recommended_calls == 10

    def test_zero_available_agents_gives_zero_recommendation(self, db):
        """Predictive: zero recommendation when no agents available."""
        decision = compute_pacing_recommendation(
            db=db,
            available_agents=0,
            reserved_agents=0,
            connected_calls=0,
            ringing_calls=0,
            override_answer_rate=0.50,
            override_talk_time=120.0,
        )
        assert decision.recommended_calls == 0

    def test_ringing_calls_reduce_recommendation(self, db):
        """Predictive: ringing calls are subtracted from recommendation."""
        decision_no_ringing = compute_pacing_recommendation(
            db=db,
            available_agents=10,
            reserved_agents=0,
            connected_calls=0,
            ringing_calls=0,
            override_answer_rate=0.50,
            override_talk_time=120.0,
        )
        decision_with_ringing = compute_pacing_recommendation(
            db=db,
            available_agents=10,
            reserved_agents=0,
            connected_calls=0,
            ringing_calls=5,
            override_answer_rate=0.50,
            override_talk_time=120.0,
        )
        assert decision_with_ringing.recommended_calls < decision_no_ringing.recommended_calls

    def test_reserved_agents_reduce_effective_capacity(self, db):
        """Predictive: reserved agents are excluded from effective capacity."""
        decision_all_available = compute_pacing_recommendation(
            db=db,
            available_agents=10,
            reserved_agents=0,
            connected_calls=0,
            ringing_calls=0,
            override_answer_rate=0.50,
            override_talk_time=120.0,
        )
        decision_some_reserved = compute_pacing_recommendation(
            db=db,
            available_agents=10,
            reserved_agents=5,
            connected_calls=0,
            ringing_calls=0,
            override_answer_rate=0.50,
            override_talk_time=120.0,
        )
        assert decision_some_reserved.recommended_calls < decision_all_available.recommended_calls

    def test_high_provider_failure_increases_recommendation(self, db):
        """Predictive: high provider failure rate increases recommendation to compensate."""
        decision_low_failure = compute_pacing_recommendation(
            db=db,
            available_agents=10,
            reserved_agents=0,
            connected_calls=0,
            ringing_calls=0,
            provider_failure_rate=0.05,
            override_answer_rate=0.50,
            override_talk_time=120.0,
        )
        decision_high_failure = compute_pacing_recommendation(
            db=db,
            available_agents=10,
            reserved_agents=0,
            connected_calls=0,
            ringing_calls=0,
            provider_failure_rate=0.40,
            override_answer_rate=0.50,
            override_talk_time=120.0,
        )
        assert decision_high_failure.recommended_calls > decision_low_failure.recommended_calls

    def test_formula_breakdown_present(self, db):
        """Predictive: formula breakdown must be included in decision."""
        decision = compute_pacing_recommendation(
            db=db,
            available_agents=5,
            reserved_agents=1,
            connected_calls=2,
            ringing_calls=1,
            override_answer_rate=0.40,
            override_talk_time=90.0,
        )
        assert "effective_available" in decision.formula_breakdown
        assert "answer_rate" in decision.formula_breakdown
        assert "recommendation" in decision.formula_breakdown
