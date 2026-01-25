{%load i18n %}📢 <b>{{context.announcement_title}}</b>

{{context.announcement_body}}
{% if context.event_url %}
<a href="{{context.event_url}}">{%trans "View Event"%}</a>
{% else %}
<a href="{{context.organization_url}}">{%trans "View Organization"%}</a>
{% endif %}
