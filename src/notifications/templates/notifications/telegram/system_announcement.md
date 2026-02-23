{%load i18n %}<b>{{context.announcement_title}}</b>

{{context.announcement_body}}
{% if context.policy_url %}
<a href="{{context.policy_url}}">{%trans "Read more"%}</a>
{% endif %}
