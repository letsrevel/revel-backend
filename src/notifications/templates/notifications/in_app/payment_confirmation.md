{% load i18n %}{% blocktranslate with event=context.event_name %}Thank you! Your payment for **{{ event }}** has been confirmed. ✅{% endblocktranslate %}

**{% trans "Payment Details:" %}**
- 💰 {% trans "Amount:" %} **{{ context.payment_amount }} {{ context.payment_currency }}**
- {% trans "Payment ID:" %} `{{ context.payment_id }}`
- {% trans "Date:" %} {{ context.payment_date }}

**{% trans "Event Details:" %}**
- 📅 {{ context.event_start_formatted }}
{% if context.event_location %}- 📍 {{ context.event_location }}{% endif %}

**{% trans "Ticket Information:" %}**
- {% trans "Tier:" %} {{ context.tier_name }}
- {% trans "Status:" %} **{% trans "Active" %}** ✅
- {% trans "Ticket ID:" %} `{{ context.ticket_id }}`

[{% trans "View Event" %}]({{ context.event_url }})
