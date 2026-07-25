"""Binary storage lifecycle helpers (delete + local cache eviction).

Purpose : quand une version est purgée/dépréciée, invalider le binaire à la SOURCE
(PVC admin en mode local, ou S3) pour couper le re-pull des pods de serving et le
raw-serve. Complété par une éviction périodique des caches locaux orphelins (C2).
"""
from __future__ import annotations

import logging
import os

from ..settings import settings

logger = logging.getLogger("device-management")


def _s3_key_from_path(s3_path: str) -> str:
    """Dérive la clé S3 depuis la valeur stockée en artifacts.s3_path.

    En mode S3 la valeur EST déjà la clé (ex. 'binaries/libreoffice/x.oxt') ; on
    la renvoie telle quelle après strip d'un éventuel préfixe absolu legacy.
    """
    rel = str(s3_path or "")
    for prefix in ("/data/content/binaries/", "/data/binaries/"):
        if rel.startswith(prefix):
            return f"{settings.s3_prefix_binaries.rstrip('/')}/{rel[len(prefix):]}"
    return rel.lstrip("/")


def delete_binary(s3_path: str) -> bool:
    """Supprime le binaire pointé par ``s3_path`` (best-effort, idempotent).

    - mode 'local' : os.remove du fichier sur le disque/PVC (chemin absolu stocké).
    - mode 'presign'/'proxy' : delete_object sur le bucket S3.
    Renvoie True si une suppression a réussi, False sinon (fichier déjà absent inclus).
    """
    if not s3_path:
        return False
    try:
        if settings.binaries_mode == "local":
            # En local, s3_path est un chemin de fichier absolu (cf. _persist_plugin_binary).
            path = s3_path if os.path.isabs(s3_path) else os.path.join(
                settings.local_binaries_dir, s3_path.lstrip("/"))
            if os.path.isfile(path):
                os.remove(path)
                logger.info("delete_binary: removed local file %s", path)
                return True
            return False
        if not settings.s3_bucket:
            return False
        from ..s3 import s3_client
        key = _s3_key_from_path(s3_path)
        s3_client().delete_object(Bucket=settings.s3_bucket, Key=key)
        logger.info("delete_binary: deleted S3 object %s", key)
        return True
    except Exception as exc:  # best-effort : la purge DB ne doit pas échouer pour ça
        logger.warning("delete_binary: failed for %s: %s", s3_path, exc)
        return False


def evict_orphan_cache(live_s3_paths: set[str]) -> int:
    """Éviction (mode local) des binaires cachés localement qui ne correspondent
    plus à aucun artifact vivant. Self-healing : borne le cache et rattrape les
    pods qui auraient raté une invalidation. Renvoie le nombre de fichiers supprimés.

    ``live_s3_paths`` = ensemble des artifacts.s3_path encore référencés (basenames
    comparés pour tolérer les variations de préfixe local/absolu entre pods).
    """
    if settings.binaries_mode != "local":
        return 0
    base = settings.local_binaries_dir
    if not os.path.isdir(base):
        return 0
    live_basenames = {os.path.basename(p) for p in live_s3_paths if p}
    removed = 0
    for root, _dirs, files in os.walk(base):
        for fn in files:
            if fn not in live_basenames:
                try:
                    os.remove(os.path.join(root, fn))
                    removed += 1
                except Exception as exc:
                    logger.warning("evict_orphan_cache: failed to remove %s: %s", fn, exc)
    if removed:
        logger.info("evict_orphan_cache: removed %d orphan cached binaries", removed)
    return removed
