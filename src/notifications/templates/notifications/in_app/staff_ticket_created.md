{% load i18n %}{% blocktranslate with holder=context.ticket_holder_name event=context.event_name %}**{{ holder }}** has registered for **{{ event }}**.{% endblocktranslate %}

**{% trans "Ticket Details:" %}**
- {% trans "Holder:" %} {{ context.ticket_holder_name }} ({{ context.ticket_holder_email }})
- {% trans "Tier:" %} {{ context.tier_name }}
- {% trans "Status:" %} {{ context.ticket_status }}
- {% trans "Ticket ID:" %} `{{ context.ticket_id }}`

[{% trans "View Event" %}]({{ context.event_url }})
