{%load i18n %}{{context.announcement_body}}
{% if context.event_url %}
[{%trans "View Event"%}]({{context.event_url}})
{% endif %}
