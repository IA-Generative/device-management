"""Version du DM sur le tableau de bord + modèle d'embedding dans le debug.

- La version affichée vient de `config_pod_state` (chaque pod upsert son
  `app_version` au heartbeat — même mécanisme que la page debug/runtime) :
  une seule source de vérité, pas de kubectl ni d'introspection d'image.
- Plusieurs versions distinctes parmi les pods FRAIS (< 15 min) = rollout en
  cours ou pod en retard → le dashboard doit lever un badge d'alerte.
- La ligne LLM du debug affiche AUSSI le modèle d'embedding (valeur effective
  du registre runtime_config, celle diffusée aux plugins).
"""
import os
from unittest import mock

from app.admin import router as admin_router


class _ScriptedCur:
    def __init__(self, rows):
        self._rows = rows
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append(" ".join(sql.split()))

    def fetchall(self):
        return self._rows


# ── _dashboard_versions ──────────────────────────────────────────────────────

def test_versions_single_version_across_pods():
    cur = _ScriptedCur([("0.9.11", 4)])
    out = admin_router._dashboard_versions(cur)
    assert out == [{"version": "0.9.11", "pods": 4}]


def test_versions_mixed_versions_ordered_by_pod_count():
    """Deux versions fraîches → les deux remontent (le template lève l'alerte)."""
    cur = _ScriptedCur([("0.9.11", 3), ("0.9.10", 1)])
    out = admin_router._dashboard_versions(cur)
    assert [v["version"] for v in out] == ["0.9.11", "0.9.10"]
    assert len(out) > 1, "versions mixtes détectées"


def test_versions_only_fresh_pods_and_null_maps_to_dev():
    """Le SQL doit filtrer sur le heartbeat (< 15 min) — les pods morts en
    attente du reaper ne doivent pas fabriquer de fausses « versions mixtes » ;
    un app_version NULL (vieux pod d'avant la colonne) s'affiche 'dev'."""
    cur = _ScriptedCur([(None, 1)])
    out = admin_router._dashboard_versions(cur)
    assert out == [{"version": "dev", "pods": 1}]
    assert "last_heartbeat_at > now() - interval '15 minutes'" in cur.executed[0]
    assert "config_pod_state" in cur.executed[0]


def test_versions_empty_table():
    """Aucun pod enregistré → liste vide, le template retombe sur l'env locale."""
    cur = _ScriptedCur([])
    assert admin_router._dashboard_versions(cur) == []


# ── _llm_models_detail (ligne LLM du debug) ──────────────────────────────────

def test_llm_detail_includes_embedding_model():
    with mock.patch.dict(os.environ, {"DEFAULT_MODEL_NAME": "octen-4b"}):
        with mock.patch("app.runtime_config.cfg", return_value="bge-multilingual-gemma2"):
            assert admin_router._llm_models_detail() == \
                "octen-4b · embed: bge-multilingual-gemma2"


def test_llm_detail_embedding_disabled_when_empty():
    with mock.patch.dict(os.environ, {"DEFAULT_MODEL_NAME": "octen-4b"}):
        with mock.patch("app.runtime_config.cfg", return_value=""):
            assert admin_router._llm_models_detail() == "octen-4b · embed: (désactivé)"


def test_llm_detail_falls_back_to_env_when_registry_unavailable():
    env = {"DEFAULT_MODEL_NAME": "octen-4b", "EMBD_MODEL_NAME": "embedding"}
    with mock.patch.dict(os.environ, env):
        with mock.patch("app.runtime_config.cfg", side_effect=RuntimeError("no db")):
            assert admin_router._llm_models_detail() == "octen-4b · embed: embedding"
