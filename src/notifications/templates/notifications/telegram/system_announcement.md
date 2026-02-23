{%load i18n %}{{context.announcement_body|striptags}}
{% if context.policy_url %}
<a href="{{context.policy_url}}">{%trans "Read more"%}</a>
{% endif %}
