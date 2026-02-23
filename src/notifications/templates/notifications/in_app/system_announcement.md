{%load i18n markdown_tags %}{{context.announcement_body|html_to_markdown}}
{% if context.policy_url %}
[{%trans "Read more"%}]({{context.policy_url}})
{% endif %}
