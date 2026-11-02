"""Responses + strict JSON-schema text models, reviewed 2026-10-30.

Sources: https://developers.openai.com/api/docs/models/<base-model>
Listed snapshots are explicit: unknown/fine-tuned/shut-down IDs fail closed.
Account visibility is checked separately; this is compatibility, not a quota guarantee.
"""

SUPPORTED_MODELS = frozenset({
    # https://developers.openai.com/api/docs/models/gpt-4.1-mini
    'gpt-4.1-mini',
    'gpt-4.1-mini-2025-04-14',
    # https://developers.openai.com/api/docs/models/gpt-4.1
    'gpt-4.1',
    'gpt-4.1-2025-04-14',
    # https://developers.openai.com/api/docs/models/gpt-4o-mini
    'gpt-4o-mini',
    'gpt-4o-mini-2024-07-18',
    # https://developers.openai.com/api/docs/models/gpt-4o
    'gpt-4o',
    'gpt-4o-2024-08-06',
    'gpt-4o-2024-11-20',
    # https://developers.openai.com/api/docs/models/gpt-5-mini
    'gpt-5-mini',
    'gpt-5-mini-2025-08-07',
    # https://developers.openai.com/api/docs/models/gpt-5-nano
    'gpt-5-nano',
    'gpt-5-nano-2025-08-07',
    # https://developers.openai.com/api/docs/models/gpt-5-pro
    'gpt-5-pro',
    'gpt-5-pro-2025-10-06',
    # https://developers.openai.com/api/docs/models/gpt-5.1
    'gpt-5.1',
    'gpt-5.1-2025-11-13',
    # https://developers.openai.com/api/docs/models/gpt-5.2
    'gpt-5.2',
    'gpt-5.2-2025-12-11',
    # https://developers.openai.com/api/docs/models/gpt-5.3-codex
    'gpt-5.3-codex',
    # https://developers.openai.com/api/docs/models/gpt-5.4-mini
    'gpt-5.4-mini',
    'gpt-5.4-mini-2026-03-17',
    # https://developers.openai.com/api/docs/models/gpt-5.4-nano
    'gpt-5.4-nano',
    'gpt-5.4-nano-2026-03-17',
    # https://developers.openai.com/api/docs/models/gpt-5.4
    'gpt-5.4',
    'gpt-5.4-2026-03-05',
    # https://developers.openai.com/api/docs/models/gpt-5.5-pro
    'gpt-5.5-pro',
    'gpt-5.5-pro-2026-04-23',
    # https://developers.openai.com/api/docs/models/gpt-5.5
    'gpt-5.5',
    'gpt-5.5-2026-04-23',
    # https://developers.openai.com/api/docs/models/gpt-5.6-luna
    'gpt-5.6-luna',
    # https://developers.openai.com/api/docs/models/gpt-5.6-sol
    'gpt-5.6-sol',
    # https://developers.openai.com/api/docs/models/gpt-5.6-terra
    'gpt-5.6-terra',
    # https://developers.openai.com/api/docs/models/gpt-5
    'gpt-5',
    'gpt-5-2025-08-07',
    # https://developers.openai.com/api/docs/models/gpt-6-astra
    'gpt-6-astra',
    # https://developers.openai.com/api/docs/models/gpt-6-luna
    'gpt-6-luna',
    # https://developers.openai.com/api/docs/models/gpt-6-sol
    'gpt-6-sol',
    # https://developers.openai.com/api/docs/models/gpt-6.1-sol
    'gpt-6.1-sol',
    # https://developers.openai.com/api/docs/models/gpt-6.1-terra
    'gpt-6.1-terra',
    # https://developers.openai.com/api/docs/models/o3
    'o3',
    'o3-2025-04-16',
})

def supported_models(models):
    return sorted({model for model in models if isinstance(model, str) and model in SUPPORTED_MODELS})
