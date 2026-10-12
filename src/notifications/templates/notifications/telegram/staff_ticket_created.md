{% load i18n %}{% blocktranslate with holder=context.ticket_holder_name event=context.event_name %}<b>{{ holder }}</b> has registered for <b>{{ event }}</b>.{% endblocktranslate %}

<b>{% trans "Ticket Details:" %}</b>
• {% trans "Holder:" %} {{ context.ticket_holder_name }}
• {% trans "Tier:" %} {{ context.tier_name }}
• {% trans "Status:" %} {{ context.ticket_status }}
• {% trans "Ticket ID:" %} <code>{{ context.ticket_id }}</code>
