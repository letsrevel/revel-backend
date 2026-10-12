{% load i18n %}{% blocktranslate with holder=context.ticket_holder_name event=context.event_name %}The ticket of **{{ holder }}** for **{{ event }}** has been refunded. 💰{% endblocktranslate %}

**{% trans "Refund Information:" %}**
- {% trans "Ticket Holder:" %} {{ context.ticket_holder_name }} ({{ context.ticket_holder_email }})
- {% trans "Amount:" %} **{{ context.refund_amount }}**
- {% trans "Ticket ID:" %} `{{ context.ticket_id }}`
