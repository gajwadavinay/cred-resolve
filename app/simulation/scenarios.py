"""
Chart Generation for Simulation Results
"""
import os
import logging
import datetime
import matplotlib
matplotlib.use("Agg")  # Non-interactive backend for server
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np
from typing import List, Dict, Any

from ..database import REPORTS_DIR

logger = logging.getLogger(__name__)


def generate_simulation_charts(results: List[Dict[str, Any]], run_prefix: str = "") -> List[str]:
    """Generate comparison charts for simulation runs. Returns list of file paths."""
    os.makedirs(REPORTS_DIR, exist_ok=True)
    ts = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    paths = []

    if not results:
        return paths

    # ── Chart 1: Calls Initiated by Scenario and Mode ─────────────────────────
    try:
        fig, axes = plt.subplots(2, 2, figsize=(14, 10))
        fig.suptitle("SmartDialer Simulation Results\nHistorical Dataset + Simulated Outcomes",
                     fontsize=14, fontweight='bold')

        # Group by scenario
        scenarios = sorted(set(r["scenario"] for r in results))
        modes = sorted(set(r["dialer_mode"] for r in results))
        colors = {"PROGRESSIVE": "#2196F3", "PREDICTIVE": "#FF5722"}

        ax = axes[0, 0]
        x = np.arange(len(scenarios))
        width = 0.35
        for i, mode in enumerate(modes):
            mode_results = [
                next((r for r in results if r["scenario"] == s and r["dialer_mode"] == mode), None)
                for s in scenarios
            ]
            initiated = [r["calls_initiated"] if r else 0 for r in mode_results]
            ax.bar(x + i * width, initiated, width, label=mode,
                   color=colors.get(mode, "#999"))
        ax.set_xlabel("Scenario")
        ax.set_ylabel("Calls Initiated")
        ax.set_title("Calls Initiated by Scenario and Mode")
        ax.set_xticks(x + width / 2)
        ax.set_xticklabels(scenarios)
        ax.legend()
        ax.grid(axis='y', alpha=0.3)

        # ── Chart 2: Answer Rate Comparison ───────────────────────────────────
        ax = axes[0, 1]
        for i, mode in enumerate(modes):
            mode_results = [
                next((r for r in results if r["scenario"] == s and r["dialer_mode"] == mode), None)
                for s in scenarios
            ]
            answered = [r["calls_answered"] if r else 0 for r in mode_results]
            initiated = [r["calls_initiated"] if r else 1 for r in mode_results]
            eff_rate = [a / max(i, 1) for a, i in zip(answered, initiated)]
            ax.plot(scenarios, eff_rate, 'o-', label=mode, color=colors.get(mode, "#999"), linewidth=2)
        ax.set_xlabel("Scenario")
        ax.set_ylabel("Effective Answer Rate (simulated)")
        ax.set_title("Effective Answer Rate by Scenario")
        ax.legend()
        ax.grid(alpha=0.3)
        ax.set_ylim(0, 1)

        # ── Chart 3: Agent Utilization ─────────────────────────────────────────
        ax = axes[1, 0]
        for i, mode in enumerate(modes):
            mode_results = [
                next((r for r in results if r["scenario"] == s and r["dialer_mode"] == mode), None)
                for s in scenarios
            ]
            util = [r["agent_utilization"] if r else 0 for r in mode_results]
            ax.bar(x + i * width, util, width, label=mode,
                   color=colors.get(mode, "#999"), alpha=0.8)
        ax.set_xlabel("Scenario")
        ax.set_ylabel("Agent Utilization")
        ax.set_title("Agent Utilization by Scenario and Mode")
        ax.set_xticks(x + width / 2)
        ax.set_xticklabels(scenarios)
        ax.legend()
        ax.grid(axis='y', alpha=0.3)
        ax.set_ylim(0, 1.1)

        # ── Chart 4: Safety Controller Decisions ──────────────────────────────
        ax = axes[1, 1]
        for i, mode in enumerate(modes):
            mode_results = [
                next((r for r in results if r["scenario"] == s and r["dialer_mode"] == mode), None)
                for s in scenarios
            ]
            approvals = [r.get("safety_approvals", 0) if r else 0 for r in mode_results]
            rejections = [r.get("safety_rejections", 0) if r else 0 for r in mode_results]
            ax.bar(x + i * width - 0.1, approvals, width * 0.45, label=f"{mode} Approved",
                   color=colors.get(mode, "#999"), alpha=0.8)
            ax.bar(x + i * width + 0.1, rejections, width * 0.45, label=f"{mode} Rejected",
                   color="#F44336", alpha=0.6)

        ax.set_xlabel("Scenario")
        ax.set_ylabel("Count")
        ax.set_title("Safety Controller Decisions")
        ax.set_xticks(x + width / 2)
        ax.set_xticklabels(scenarios)
        ax.legend(fontsize=7)
        ax.grid(axis='y', alpha=0.3)

        plt.tight_layout()
        path = os.path.join(REPORTS_DIR, f"{ts}_simulation_comparison.png")
        plt.savefig(path, dpi=120, bbox_inches='tight')
        plt.close()
        paths.append(path)
        logger.info(f"Chart saved: {path}")
    except Exception as e:
        logger.error(f"Chart 1 error: {e}")

    # ── Chart 2: Scenario D Dynamic ───────────────────────────────────────────
    try:
        d_results = [r for r in results if r.get("scenario") == "D"]
        if d_results:
            r = d_results[0]
            pacing = r.get("pacing_decisions", [])
            if pacing:
                steps = [p["step"] for p in pacing]
                recs = [p.get("recommendation", 0) for p in pacing]
                rates = [p.get("answer_rate", 0) for p in pacing]

                fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8))
                fig.suptitle("Scenario D: Dynamic Conditions Over Time", fontsize=13)

                ax1.plot(steps, recs, 'b-o', label="Calls Recommended", linewidth=2)
                ax1.set_ylabel("Calls Recommended")
                ax1.set_title("Pacing Decisions over Simulation Steps")
                ax1.grid(alpha=0.3)
                ax1.legend()

                ax2.plot(steps, rates, 'r-o', label="Answer Rate (simulated)", linewidth=2)
                ax2.set_xlabel("Simulation Step")
                ax2.set_ylabel("Answer Rate")
                ax2.set_title("Answer Rate Changes (Scenario D)")
                ax2.grid(alpha=0.3)
                ax2.legend()
                ax2.set_ylim(0, 1)

                plt.tight_layout()
                path2 = os.path.join(REPORTS_DIR, f"{ts}_scenario_d_dynamic.png")
                plt.savefig(path2, dpi=120, bbox_inches='tight')
                plt.close()
                paths.append(path2)
    except Exception as e:
        logger.error(f"Chart 2 error: {e}")

    return paths
