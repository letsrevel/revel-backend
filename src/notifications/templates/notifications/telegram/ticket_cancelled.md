{% load i18n %}❌ {% blocktranslate with event=context.event_name %}Your ticket for <b>{{ event }}</b> has been cancelled.{% endblocktranslate %}

<b>{% trans "Event Details:" %}</b>
📅 {{ context.event_start_formatted }}
{% if context.event_location %}📍 {{ context.event_location }}{% endif %}

<b>{% trans "Cancelled Ticket:" %}</b>
• {% trans "Tier:" %} {{ context.tier_name }}
• {% trans "Ticket ID:" %} <code>{{ context.ticket_id }}</code>

{% if context.cancellation_reason %}
<b>{% trans "Reason:" %}</b> {{ context.cancellation_reason }}
{% endif %}

{% if context.refund_amount %}
💰 {% blocktranslate with amount=context.refund_amount currency=context.payment_currency %}Refund of {{ amount }} {{ currency }} will be processed within 5-10 business days.{% endblocktranslate %}
{% endif %}

<a href="{{ context.event_url }}">{% trans "View Event" %}</a>
