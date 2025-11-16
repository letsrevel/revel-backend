{% load i18n %}{% if context.is_organizer and context.actor_name %}
✅ {% blocktranslate with actor=context.actor_name item=context.item_name event=context.event_name %}<b>{{ actor }}</b> claimed <b>"{{ item }}"</b> for {{ event }}.{% endblocktranslate %}
{% else %}
✅ {% blocktranslate with item=context.item_name event=context.event_name %}<b>"{{ item }}"</b> has been claimed for {{ event }}.{% endblocktranslate %}
{% endif %}

<b>{% trans "Item Details:" %}</b>
• {% trans "Item:" %} {{ context.item_name }}
• {% trans "Category:" %} {{ context.item_type }}
{% if context.quantity %}• {% trans "Quantity:" %} {{ context.quantity }}{% endif %}
{% if context.is_organizer and context.actor_name %}• {% trans "Claimed by:" %} {{ context.actor_name }}{% endif %}

<a href="{{ context.frontend_url }}">{% trans "View Potluck List" %}</a>
