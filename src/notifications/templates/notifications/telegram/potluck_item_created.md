{% load i18n %}{% if context.is_organizer and context.actor_name %}
🍽️ {% blocktranslate with actor=context.actor_name item=context.item_name event=context.event_name %}<b>{{ actor }}</b> added <b>"{{ item }}"</b> to the potluck for {{ event }}.{% endblocktranslate %}
{% else %}
🍽️ {% blocktranslate with item=context.item_name event=context.event_name %}New potluck item <b>"{{ item }}"</b> added to <b>{{ event }}</b>.{% endblocktranslate %}
{% endif %}

<b>{% trans "Item Details:" %}</b>
• {% trans "Item:" %} {{ context.item_name }}
• {% trans "Category:" %} {{ context.item_type }}
{% if context.quantity %}• {% trans "Quantity:" %} {{ context.quantity }}{% endif %}
{% if context.actor_name %}• {% trans "Added by:" %} {{ context.actor_name }}{% endif %}

{% if context.note %}
<b>{% trans "Description:" %}</b> {{ context.note }}
{% endif %}

<a href="{{ context.event_url }}">{% trans "Claim Item" %}</a>
