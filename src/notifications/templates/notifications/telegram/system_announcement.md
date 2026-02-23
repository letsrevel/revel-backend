{%load i18n markdown_tags %}{{context.announcement_body|html_to_text}}
{% if context.policy_url %}
<a href="{{context.policy_url}}">{%trans "Read more"%}</a>
{% endif %}
