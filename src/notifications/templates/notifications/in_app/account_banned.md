{%load i18n %}{%trans "Your account has been suspended for violating our Terms of Service." %}
{% if context.ban_reason %}

**{%trans "Reason" %}:** {{context.ban_reason|escape}}
{% endif %}
