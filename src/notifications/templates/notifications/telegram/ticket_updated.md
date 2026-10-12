{% load i18n %}{% if context.old_status == "pending" and context.new_status == "active" %}✅ {% blocktranslate with event=context.event_name %}Ticket Confirmed for <b>{{ event }}</b>{% endblocktranslate %}

<b>{% trans "Event Details:" %}</b>
📅 {{ context.event_start_formatted }}
{% if context.event_location %}📍 {{ context.event_location }}{% endif %}

<b>{% trans "Ticket Information:" %}</b>
• {% trans "Tier:" %} {{ context.tier_name }}
• {% trans "Status:" %} {% trans "Active" %}

{% trans "Your payment has been confirmed!" %}
{% else %}🔄 {% blocktranslate with event=context.event_name %}Ticket Update for <b>{{ event }}</b>{% endblocktranslate %}

<b>{% trans "Event Details:" %}</b>
📅 {{ context.event_start_formatted }}
{% if context.event_location %}📍 {{ context.event_location }}{% endif %}

<b>{% trans "Updated Information:" %}</b>
• {% trans "Tier:" %} {{ context.tier_name }}
• {% trans "Status:" %} {{ context.ticket_status }}

{% if context.reason %}
<b>{% trans "Reason:" %}</b> {{ context.reason }}
{% endif %}{% endif %}
