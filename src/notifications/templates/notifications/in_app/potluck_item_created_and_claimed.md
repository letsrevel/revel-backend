{% load i18n %}{% blocktranslate with item=context.item_name event=context.event_name %}🍽️✅ New potluck item **"{{ item }}"** added and claimed for **{{ event }}**.{% endblocktranslate %}

**{% trans "Item Details:" %}**
- {% trans "Item:" %} {{ context.item_name }}
- {% trans "Category:" %} {{ context.item_type }}
{% if context.quantity %}- {% trans "Quantity:" %} {{ context.quantity }}{% endif %}
{% if context.actor_name %}- {% trans "Added and claimed by:" %} {{ context.actor_name }}{% endif %}

{% if context.note %}
**{% trans "Description:" %}** {{ context.note }}
{% endif %}

[{% trans "View Potluck List" %}]({{ context.event_url }})
