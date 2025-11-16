{% load i18n %}{% if context.ticket_holder_name %}
{% blocktranslate with holder=context.ticket_holder_name event=context.event_name %}<b>{{ holder }}</b> has registered for <b>{{ event }}</b>.{% endblocktranslate %}

<b>{% trans "Ticket Details:" %}</b>
• {% trans "Holder:" %} {{ context.ticket_holder_name }}
• {% trans "Tier:" %} {{ context.tier_name }}
• {% trans "Status:" %} {{ context.ticket_status }}
• {% trans "Ticket ID:" %} <code>{{ context.ticket_id }}</code>

{% else %}
🎫 {% blocktranslate with event=context.event_name %}Your ticket for <b>{{ event }}</b> is confirmed!{% endblocktranslate %}

<b>{% trans "Event Details:" %}</b>
📅 {{ context.event_start_formatted }}
{% if context.event_location %}📍 {{ context.event_location }}{% endif %}

<b>{% trans "Ticket Information:" %}</b>
• {% trans "Tier:" %} {{ context.tier_name }}
• {% trans "Status:" %} {{ context.ticket_status }}

{% if context.ticket_status == "pending_payment" %}
⚠️ <b>{% trans "Payment Required" %}</b>
{% blocktranslate with amount=context.payment_amount currency=context.payment_currency %}Please complete payment of {{ amount }} {{ currency }}.{% endblocktranslate %}
{% endif %}
{% endif %}

<a href="{{ context.event_url }}">{% trans "View Event" %}</a>
