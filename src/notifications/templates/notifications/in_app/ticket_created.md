{% load i18n markdown_tags %}{% if context.ticket_status == "pending" %}{% blocktranslate with event=context.event_name %}Your ticket for **{{ event }}** is pending payment confirmation. ⏳{% endblocktranslate %}

**{% trans "Event Details:" %}**
- 📅 {{ context.event_start_formatted }}
{% if context.event_location %}- 📍 {{ context.event_location }}{% endif %}

**{% trans "Ticket Information:" %}**
- {% trans "Tier:" %} {{ context.tier_name }}
- {% trans "Status:" %} {% trans "Pending" %}
- {% trans "Ticket ID:" %} `{{ context.ticket_id }}`

{% if context.manual_payment_instructions %}**{% trans "Payment Instructions:" %}**
> {{ context.manual_payment_instructions|strip_leading_heading }}
{% else %}_{% trans "Please contact the organizer to complete the payment." %}_
{% endif %}{% else %}{% blocktranslate with event=context.event_name %}Your ticket for **{{ event }}** is confirmed! 🎉{% endblocktranslate %}

**{% trans "Event Details:" %}**
- 📅 {{ context.event_start_formatted }}
{% if context.event_location %}- 📍 {{ context.event_location }}{% endif %}

**{% trans "Ticket Information:" %}**
- {% trans "Tier:" %} {{ context.tier_name }}
- {% trans "Status:" %} {% trans "Active" %}
- {% trans "Ticket ID:" %} `{{ context.ticket_id }}`
{% endif %}

[{% trans "View Event" %}]({{ context.event_url }})
