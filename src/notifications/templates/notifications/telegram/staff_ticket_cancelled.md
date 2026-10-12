{% load i18n %}❌ {% if context.cancellation_source == "user" %}{% blocktranslate with holder=context.ticket_holder_name event=context.event_name %}<b>{{ holder }}</b> cancelled their ticket for <b>{{ event }}</b>.{% endblocktranslate %}{% elif context.cancellation_source == "stripe_dashboard" %}{% blocktranslate with holder=context.ticket_holder_name event=context.event_name %}<b>{{ holder }}</b>'s ticket for <b>{{ event }}</b> was refunded via the Stripe dashboard.{% endblocktranslate %}{% else %}{% blocktranslate with holder=context.ticket_holder_name event=context.event_name %}<b>{{ holder }}</b>'s ticket for <b>{{ event }}</b> has been cancelled.{% endblocktranslate %}{% endif %}

<b>{% trans "Ticket Holder:" %}</b> {{ context.ticket_holder_name }} ({{ context.ticket_holder_email }})

<b>{% trans "Cancelled Ticket:" %}</b>
• {% trans "Tier:" %} {{ context.tier_name }}
• {% trans "Ticket ID:" %} <code>{{ context.ticket_id }}</code>

{% if context.cancellation_reason %}
<b>{% trans "Reason:" %}</b> {{ context.cancellation_reason }}
{% endif %}

{% if context.refund_amount %}
💰 {% blocktranslate with amount=context.refund_amount currency=context.payment_currency %}Refund of {{ amount }} {{ currency }} issued to the ticket holder.{% endblocktranslate %}
{% endif %}

<a href="{{ context.event_url }}">{% trans "View Event" %}</a>
