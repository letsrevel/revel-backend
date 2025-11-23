{% load i18n %}{% if context.action == "deleted" %}
🗑️ {% blocktranslate with item=context.item_name event=context.event_name %}<b>"{{ item }}"</b> has been removed from the potluck for {{ event }}.{% endblocktranslate %}
{% else %}
🔄 {% blocktranslate with item=context.item_name event=context.event_name %}<b>"{{ item }}"</b> has been updated for {{ event }}.{% endblocktranslate %}
{% endif %}

<a href="{{ context.event_url }}">{% trans "View Potluck List" %}</a>
