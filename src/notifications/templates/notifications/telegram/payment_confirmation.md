{% load i18n %}✅ {% blocktranslate with event=context.event_name %}Payment confirmed for <b>{{ event }}</b>!{% endblocktranslate %}

<b>{% trans "Payment Details:" %}</b>
💰 {{ context.payment_amount }} {{ context.payment_currency }}
• {% trans "Payment ID:" %} <code>{{ context.payment_id }}</code>
• {% trans "Date:" %} {{ context.payment_date }}

<b>{% trans "Event Details:" %}</b>
📅 {{ context.event_start_formatted }}
{% if context.event_location %}📍 {{ context.event_location }}{% endif %}

<b>{% trans "Ticket Information:" %}</b>
• {% trans "Tier:" %} {{ context.tier_name }}
• {% trans "Status:" %} <b>{% trans "Active" %}</b> ✅

{% trans "Your ticket is now active!" %}

<a href="{{ context.event_url }}">{% trans "View Event" %}</a>
