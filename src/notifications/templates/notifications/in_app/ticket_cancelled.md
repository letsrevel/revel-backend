{% load i18n %}{% if context.ticket_holder_name %}{% if context.cancellation_source == "user" %}{% blocktranslate with holder=context.ticket_holder_name event=context.event_name %}**{{ holder }}** cancelled their ticket for **{{ event }}**.{% endblocktranslate %}{% elif context.cancellation_source == "stripe_dashboard" %}{% blocktranslate with holder=context.ticket_holder_name event=context.event_name %}**{{ holder }}**'s ticket for **{{ event }}** was refunded via the Stripe dashboard.{% endblocktranslate %}{% else %}{% blocktranslate with holder=context.ticket_holder_name event=context.event_name %}**{{ holder }}**'s ticket for **{{ event }}** has been cancelled.{% endblocktranslate %}{% endif %}

**{% trans "Ticket Holder:" %}** {{ context.ticket_holder_name }} ({{ context.ticket_holder_email }}){% else %}{% if context.cancellation_source == "user" %}{% blocktranslate with event=context.event_name %}You cancelled your ticket for **{{ event }}**.{% endblocktranslate %}{% elif context.cancellation_source == "stripe_dashboard" %}{% blocktranslate with event=context.event_name %}Your ticket for **{{ event }}** has been cancelled and refunded.{% endblocktranslate %}{% else %}{% blocktranslate with event=context.event_name %}Your ticket for **{{ event }}** has been cancelled.{% endblocktranslate %}{% endif %}{% endif %}

**{% trans "Event Details:" %}**
- 📅 {{ context.event_start_formatted }}
{% if context.event_location %}- 📍 {{ context.event_location }}{% endif %}

**{% trans "Cancelled Ticket:" %}**
- {% trans "Tier:" %} {{ context.tier_name }}
- {% trans "Ticket ID:" %} `{{ context.ticket_id }}`

{% if context.cancellation_reason %}
**{% trans "Reason:" %}** {{ context.cancellation_reason }}
{% endif %}

{% if context.refund_amount %}
{% if context.ticket_holder_name %}💰 {% blocktranslate with amount=context.refund_amount currency=context.payment_currency %}A refund of **{{ amount }} {{ currency }}** has been issued to the ticket holder.{% endblocktranslate %}{% else %}💰 {% blocktranslate with amount=context.refund_amount currency=context.payment_currency %}A refund of **{{ amount }} {{ currency }}** will be processed within 5-10 business days.{% endblocktranslate %}{% endif %}
{% endif %}

[{% trans "View Event" %}]({{ context.event_url }})
