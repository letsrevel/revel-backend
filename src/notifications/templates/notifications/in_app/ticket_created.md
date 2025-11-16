{% load i18n %}{% if context.ticket_holder_name %}
{% blocktranslate with holder=context.ticket_holder_name event=context.event_name %}**{{ holder }}** has registered for **{{ event }}**.{% endblocktranslate %}

**{% trans "Ticket Details:" %}**
- {% trans "Holder:" %} {{ context.ticket_holder_name }} ({{ context.ticket_holder_email }})
- {% trans "Tier:" %} {{ context.tier_name }}
- {% trans "Status:" %} {{ context.ticket_status }}
- {% trans "Ticket ID:" %} `{{ context.ticket_id }}`

{% else %}
{% blocktranslate with event=context.event_name %}Your ticket for **{{ event }}** is confirmed! 🎉{% endblocktranslate %}

**{% trans "Event Details:" %}**
- 📅 {{ context.event_start_formatted }}
{% if context.event_location %}- 📍 {{ context.event_location }}{% endif %}

**{% trans "Ticket Information:" %}**
- {% trans "Tier:" %} {{ context.tier_name }}
- {% trans "Status:" %} {{ context.ticket_status }}
- {% trans "Ticket ID:" %} `{{ context.ticket_id }}`

{% if context.ticket_status == "pending_payment" %}
⚠️ **{% trans "Payment Required:" %}** {% blocktranslate with amount=context.payment_amount currency=context.payment_currency %}Please complete your payment of {{ amount }} {{ currency }} to activate your ticket.{% endblocktranslate %}
{% endif %}
{% endif %}

[{% trans "View Event" %}]({{ context.event_url }})
