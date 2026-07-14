"""Lecture des compteurs de trafic LLM (table llm_traffic, buckets 5 min).

Écriture côté proxy : app/llm/traffic.py (accumulateur mémoire + flush UPSERT).
Ici : les agrégats servis au dashboard admin — série temporelle chat vs
embeddings et tuiles 24 h. Périodes alignées sur le widget adoption.
"""
from __future__ import annotations

# période → (intervalle SQL, granularité date_trunc) — whitelist stricte,
# jamais d'interpolation de l'entrée utilisateur dans le SQL.
_PERIODS: dict[str, tuple[str, str]] = {
    "1J": ("24 hours", "hour"),
    "1S": ("7 days", "day"),
    "1M": ("30 days", "day"),
    "3M": ("90 days", "day"),
}


def series(cur, period: str = "1J") -> list[dict]:
    """Série temporelle des appels par sous-bucket : chat, embeddings, erreurs."""
    interval, trunc = _PERIODS.get(period, _PERIODS["1J"])
    cur.execute(
        """
        SELECT date_trunc(%s, bucket_ts) AS t,
               COALESCE(SUM(count) FILTER (WHERE route = 'chat'), 0) AS chat,
               COALESCE(SUM(count) FILTER (WHERE route = 'embeddings'), 0) AS embeddings,
               COALESCE(SUM(count) FILTER (WHERE status_class <> '2xx'), 0) AS errors
        FROM llm_traffic
        WHERE bucket_ts > now() - %s::interval
        GROUP BY 1
        ORDER BY 1
        """,
        (trunc, interval),
    )
    return [
        {"date": row[0].isoformat(), "chat": int(row[1]),
         "embeddings": int(row[2]), "errors": int(row[3])}
        for row in cur.fetchall()
    ]


def tiles(cur) -> dict:
    """Tuiles 24 h : volume, latence moyenne, taux d'erreur, part embeddings."""
    cur.execute(
        """
        SELECT COALESCE(SUM(count), 0),
               COALESCE(SUM(duration_ms_sum), 0),
               COALESCE(SUM(count) FILTER (WHERE status_class <> '2xx'), 0),
               COALESCE(SUM(count) FILTER (WHERE route = 'embeddings'), 0),
               COALESCE(SUM(tokens_sum), 0)
        FROM llm_traffic
        WHERE bucket_ts > now() - interval '24 hours'
        """
    )
    total, dur_sum, errors, embeddings, tokens = cur.fetchone()
    total = int(total)
    return {
        "calls_24h": total,
        "avg_ms": round(int(dur_sum) / total) if total else 0,
        "error_rate": round(int(errors) / total * 100, 1) if total else 0,
        "embed_share": round(int(embeddings) / total * 100) if total else 0,
        "tokens_24h": int(tokens),
    }


def error_rate_24h(cur) -> float:
    """Taux d'erreur LLM 24 h — la tuile « Taux d'erreur » du dashboard
    (remplace le 0 codé en dur historique)."""
    return tiles(cur)["error_rate"]
