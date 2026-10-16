"""Erreurs métier du pipeline."""


class IngestionError(RuntimeError):
    """Erreur métier : le message est déjà rédigé pour l'utilisateur."""


class QuotaAbort(IngestionError):
    """Arrêt propre sur quota (crédits épuisés) : caches conservés, reprise possible."""
