{% load i18n %}🔓 {% blocktranslate with item=context.item_name event=context.event_name %}<b>"{{ item }}"</b> is now available for {{ event }}.{% endblocktranslate %}

<b>{% trans "Item Details:" %}</b>
• {% trans "Item:" %} {{ context.item_name }}
• {% trans "Category:" %} {{ context.item_type }}
{% if context.quantity %}• {% trans "Quantity:" %} {{ context.quantity }}{% endif %}

<a href="{{ context.event_url }}">{% trans "Claim Item" %}</a>
