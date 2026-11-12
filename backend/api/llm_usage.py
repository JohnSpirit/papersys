"""LLM 用量统计（llm_calls 表的聚合读）。

数据源：backend/external/llm.py 每次调用落一行（2026-10-17 起）。
token 数依赖提供方返回 usage；为 NULL 的行按 0 计入（字符数恒有，
可用 prompt_chars/4 粗估 token）。

费用折算（cached_tokens 2026-10-17 加入；价签在 config.toml [llm]，
单位元/百万 tokens）：
  input_cost   = (prompt_tokens - cached_tokens) * price_input_per_mtok
  cached_cost  = cached_tokens * price_cached_input_per_mtok
  output_cost  = completion_tokens * price_output_per_mtok
cached_tokens 为 NULL（提供方不回缓存字段）时按 0 计——费用按非缓存价算，可能高估。
"""
from flask import Blueprint, jsonify

from backend.config import CONFIG
from backend.db.conn import get_conn

bp = Blueprint("llm_usage", __name__, url_prefix="/api")

_COLS = """COUNT(*) AS calls,
           SUM(CASE WHEN ok=1 THEN prompt_tokens END) AS pt,
           SUM(CASE WHEN ok=1 THEN completion_tokens END) AS ct,
           SUM(CASE WHEN ok=1 THEN cached_tokens END) AS cached,
           SUM(CASE WHEN ok=1 THEN prompt_chars END) AS pc,
           SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END) AS errs"""


def _prices() -> tuple[float, float, float]:
    cfg = CONFIG.get("llm", {})
    return (
        float(cfg.get("price_input_per_mtok", 0)),
        float(cfg.get("price_cached_input_per_mtok", 0)),
        float(cfg.get("price_output_per_mtok", 0)),
    )


def _row_to_dict(r, p_in: float, p_cached: float, p_out: float) -> dict:
    pt = r["pt"] or 0
    ct = r["ct"] or 0
    cached = r["cached"] or 0
    input_cost = (pt - cached) * p_in / 1e6
    cached_cost = cached * p_cached / 1e6
    output_cost = ct * p_out / 1e6
    return {
        "calls": r["calls"] or 0,
        "prompt_tokens": pt,
        "completion_tokens": ct,
        "cached_tokens": cached,
        "prompt_chars": r["pc"] or 0,
        "errors": r["errs"] or 0,
        "cost_input": round(input_cost, 6),
        "cost_cached": round(cached_cost, 6),
        "cost_output": round(output_cost, 6),
        "cost_total": round(input_cost + cached_cost + output_cost, 6),
    }


@bp.get("/llm_usage")
def llm_usage():
    p_in, p_cached, p_out = _prices()
    conn = get_conn()
    try:
        totals = conn.execute(f"SELECT {_COLS} FROM llm_calls").fetchone()
        recent = conn.execute(
            f"SELECT {_COLS} FROM llm_calls"
            " WHERE ts >= datetime('now', '-30 days')"
        ).fetchone()
        by_kind = conn.execute(
            f"SELECT kind, {_COLS} FROM llm_calls"
            " GROUP BY kind ORDER BY calls DESC"
        ).fetchall()
        by_model = conn.execute(
            f"SELECT model, {_COLS} FROM llm_calls"
            " GROUP BY model ORDER BY calls DESC"
        ).fetchall()
    finally:
        conn.close()

    return jsonify({
        "prices": {"input_per_mtok": p_in, "cached_input_per_mtok": p_cached,
                   "output_per_mtok": p_out},
        "totals":    _row_to_dict(totals, p_in, p_cached, p_out),
        "recent30d": _row_to_dict(recent, p_in, p_cached, p_out),
        "by_kind":   [{"kind": r["kind"] or "unknown",
                       **_row_to_dict(r, p_in, p_cached, p_out)} for r in by_kind],
        "by_model":  [{"model": r["model"] or "unknown",
                       **_row_to_dict(r, p_in, p_cached, p_out)} for r in by_model],
    })
