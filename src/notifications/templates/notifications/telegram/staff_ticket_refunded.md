{% load i18n %}💰 {% blocktranslate with holder=context.ticket_holder_name event=context.event_name %}The ticket of <b>{{ holder }}</b> for <b>{{ event }}</b> has been refunded.{% endblocktranslate %}

<b>{% trans "Refund Information:" %}</b>
• {% trans "Ticket Holder:" %} {{ context.ticket_holder_name }}
• {% trans "Amount:" %} <b>{{ context.refund_amount }}</b>
• {% trans "Ticket ID:" %} <code>{{ context.ticket_id }}</code>
