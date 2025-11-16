{% load i18n %}💰 {% blocktranslate with event=context.event_name %}Your ticket refund for <b>{{ event }}</b> has been processed.{% endblocktranslate %}

<b>{% trans "Event Details:" %}</b>
📅 {{ context.event_start_formatted }}
{% if context.event_location %}📍 {{ context.event_location }}{% endif %}

<b>{% trans "Refund Information:" %}</b>
• {% trans "Amount:" %} <b>{{ context.refund_amount }} {{ context.payment_currency }}</b>
• {% trans "Ticket ID:" %} <code>{{ context.ticket_id }}</code>
• {% trans "Tier:" %} {{ context.tier_name }}

{% if context.refund_reason %}
<b>{% trans "Reason:" %}</b> {{ context.refund_reason }}
{% endif %}

{% blocktranslate %}Refund will appear within 5-10 business days.{% endblocktranslate %}

<a href="{{ context.event_url }}">{% trans "View Event" %}</a>
