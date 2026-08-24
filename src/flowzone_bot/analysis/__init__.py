"""Аналитика flowzone_bot: Volume Profile, контекст аукциона, зоны, триггер.

Заполняется по фазам реализации (TASKSPEC §11):
- фаза 2: volume_profile (POC/VAH/VAL/HVN/LVN/ledge) + context (тренд vs баланс)
- фаза 3: delta-at-price + big-trades detector + absorption trigger
- фаза 4: zone builder (confluence)
- фаза 5: trade manager (стоп за зоной, цели, reload)
"""
