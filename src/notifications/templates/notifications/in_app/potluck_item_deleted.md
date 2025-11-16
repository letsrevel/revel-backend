{% load i18n %}🗑️ {% blocktranslate with item=context.item_name event=context.event_name %}"{{ item }}" has been removed from the potluck for {{ event }}.{% endblocktranslate %}

[{% trans "View Potluck List" %}]({{ context.frontend_url }})
