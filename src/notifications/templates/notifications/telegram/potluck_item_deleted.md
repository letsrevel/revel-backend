{% load i18n %}🗑️ {% blocktranslate with item=context.item_name event=context.event_name %}<b>"{{ item }}"</b> has been removed from the potluck for {{ event }}.{% endblocktranslate %}

<a href="{{ context.frontend_url }}">{% trans "View Potluck List" %}</a>
