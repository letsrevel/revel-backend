{% load i18n %}{% blocktranslate with item=context.item_name event=context.event_name %}🍽️ New potluck item **"{{ item }}"** added to **{{ event }}**.{% endblocktranslate %}

**{% trans "Item Details:" %}**
- {% trans "Item:" %} {{ context.item_name }}
- {% trans "Category:" %} {{ context.item_category }}
- {% trans "Quantity:" %} {{ context.quantity_needed }}
{% if context.created_by_name %}- {% trans "Added by:" %} {{ context.created_by_name }}{% endif %}

{% if context.item_description %}
**{% trans "Description:" %}** {{ context.item_description }}
{% endif %}

[{% trans "Claim Item" %}]({{ context.event_url }})
