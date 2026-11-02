"""Local diagnostics report field names only. Never format configuration values/errors."""
from app.features.mailboxes.infrastructure.crypto import TokenCipher

CONNECTION_FIELDS = ('supabase_url','supabase_publishable_key','supabase_secret_key',
                     'gmail_client_id','gmail_client_secret','google_token_encryption_key')


def configuration_report(settings, *, receiver=False):
    missing = [name.upper() for name in CONNECTION_FIELDS if not getattr(settings,name)]
    invalid = []
    for name, prefix in (('supabase_secret_key', 'sb_secret_'),
                         ('supabase_publishable_key', 'sb_publishable_')):
        value = getattr(settings, name)
        if value and not value.get_secret_value().startswith(prefix):
            invalid.append(name.upper())
    if settings.supabase_url and settings.supabase_url.host.casefold() == 'your_project.supabase.co':
        invalid.append('SUPABASE_URL')
    if settings.google_token_encryption_key:
        try:
            TokenCipher(settings.google_token_encryption_key)
        except Exception:
            invalid.append('GOOGLE_TOKEN_ENCRYPTION_KEY')
    if settings.worker_enabled:
        invalid.append('WORKER_ENABLED')
    if receiver:
        for name in ('llm_base_url','llm_api_key'):
            if not getattr(settings,name): missing.append(name.upper())
        if not (settings.llm_classification_model or settings.llm_model):
            missing.append('LLM_CLASSIFICATION_MODEL_OR_LLM_MODEL')
    push_fields = ('gmail_topic', 'gmail_subscription', 'pubsub_audience', 'pubsub_service_account')
    if any(getattr(settings, name) for name in push_fields):
        missing.extend(name.upper() for name in push_fields if not getattr(settings, name))
    return {'status':'ready' if not missing and not invalid else 'not_ready',
            'missing':missing,'invalid':invalid}


def require_startup_configuration(settings):
    report = configuration_report(settings, receiver=settings.google_mailbox_worker_enabled)
    fields = sorted(set(report['missing'] + report['invalid']))
    if fields:
        # SystemExit avoids Pydantic/SDK exception rendering of configuration values.
        raise SystemExit(', '.join(sorted(set(fields))))
