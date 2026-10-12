{% load i18n %}{% if context.old_status == "pending" and context.new_status == "active" %}{% blocktranslate with event=context.event_name %}Your ticket for **{{ event }}** has been confirmed! 🎉{% endblocktranslate %}

**{% trans "Event Details:" %}**
- 📅 {{ context.event_start_formatted }}
{% if context.event_location %}- 📍 {{ context.event_location }}{% endif %}

**{% trans "Ticket Information:" %}**
- {% trans "Tier:" %} {{ context.tier_name }}
- {% trans "Status:" %} {% trans "Active" %}
- {% trans "Ticket ID:" %} `{{ context.ticket_id }}`

{% trans "Your payment has been confirmed. Your ticket is now active!" %}
{% else %}{% blocktranslate with event=context.event_name %}Your ticket for **{{ event }}** has been updated. 🔄{% endblocktranslate %}

**{% trans "Event Details:" %}**
- 📅 {{ context.event_start_formatted }}
{% if context.event_location %}- 📍 {{ context.event_location }}{% endif %}

**{% trans "Updated Ticket Information:" %}**
- {% trans "Tier:" %} {{ context.tier_name }}
- {% trans "Status:" %} {{ context.ticket_status }}
- {% trans "Ticket ID:" %} `{{ context.ticket_id }}`

{% if context.reason %}
**{% trans "Reason:" %}** {{ context.reason }}
{% endif %}{% endif %}

[{% trans "View Event" %}]({{ context.event_url }})
