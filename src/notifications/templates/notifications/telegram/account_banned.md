{%load i18n %}{%trans "Your account has been suspended for violating our Terms of Service." %}
{% if context.ban_reason %}

<b>{%trans "Reason" %}:</b> {{context.ban_reason|escape}}
{% endif %}
