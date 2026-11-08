"""Translate engine-provided status text into the dashboard language.

The verified Freqtrade Go binary still emits a few French status strings. They
are localized here, at the UI boundary, so the dashboard stays English without
invalidating the signed binary. Keep this table in sync with the engine when it
is rebuilt.
"""

_ENGINE_TEXT = (
    ("Comptabilite incomplete: executions finales non confirmees",
     "Incomplete accounting: final executions not confirmed"),
    ("Arret confirme: ordres et position a zero", "Stop confirmed: orders and position at zero"),
    ("Arret non confirme: ", "Stop not confirmed: "),
    ("FREQTRADE | attente carnet valide", "FREQTRADE | waiting for a valid book"),
    ("FREQTRADE | moteur actif", "FREQTRADE | engine active"),
    ("FREQTRADE | execution suspendue: ", "FREQTRADE | execution suspended: "),
    ("budget API insuffisant", "insufficient API budget"),
    ("requetes restantes", "requests remaining"),
    ("quotes suspendues : ", "quotes suspended: "),
    ("fonds disponibles non confirmes", "available funds not confirmed"),
    ("attente capacite inventaire / montant executable",
     "waiting for inventory capacity / executable amount"),
    ("preparation des ordres", "preparing orders"),
    ("position non confirmee", "position not confirmed"),
    ("reponse position invalide", "invalid position response"),
    ("symbole de position invalide", "invalid position symbol"),
    ("taille de position invalide", "invalid position size"),
    ("position residuelle", "residual position"),
    ("ordres non confirmes", "orders not confirmed"),
    ("reponse ordres invalide", "invalid orders response"),
    ("symbole d'ordre invalide", "invalid order symbol"),
    ("ordres residuels", "residual orders"),
)

TEXT_FIELDS = ("action", "error", "cleanup_error", "recovery_error", "warning")


def localize_state(state):
    """Return ``state`` with engine-provided status text translated in place."""
    for key in TEXT_FIELDS:
        value = state.get(key)
        if isinstance(value, str):
            for source, target in _ENGINE_TEXT:
                value = value.replace(source, target)
            state[key] = value
    return state
