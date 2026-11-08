"""A target stops new purchases; the existing maker drain confirms the exit."""
def finish_reason(config, current_volume, now):
    deadline = config.get('session_expires_at')
    if deadline is not None and now >= deadline:
        return 'Session duration reached: finishing maker exit'
    target = config.get('target_volume_usd')
    if target is not None and current_volume is not None:
        if config.get('session_volume_carry', 0)+current_volume >= target:
            return 'Session volume reached: finishing maker exit'
    return None
