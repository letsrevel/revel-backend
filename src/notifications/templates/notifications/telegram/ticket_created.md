{% load i18n markdown_tags %}{% if context.ticket_status == "pending" %}⏳ {% blocktranslate with event=context.event_name %}Ticket Pending for <b>{{ event }}</b>{% endblocktranslate %}

<b>{% trans "Event Details:" %}</b>
📅 {{ context.event_start_formatted }}
{% if context.event_location %}📍 {{ context.event_location }}{% endif %}

<b>{% trans "Ticket Information:" %}</b>
• {% trans "Tier:" %} {{ context.tier_name }}
• {% trans "Status:" %} {% trans "Pending" %}

{% if context.manual_payment_instructions %}<b>{% trans "Payment Instructions:" %}</b>
<blockquote>{{ context.manual_payment_instructions|strip_leading_heading }}</blockquote>
{% else %}<i>{% trans "Please contact the organizer to complete the payment." %}</i>
{% endif %}{% else %}✅ {% blocktranslate with event=context.event_name %}Ticket Confirmed for <b>{{ event }}</b>{% endblocktranslate %}

<b>{% trans "Event Details:" %}</b>
📅 {{ context.event_start_formatted }}
{% if context.event_location %}📍 {{ context.event_location }}{% endif %}

<b>{% trans "Ticket Information:" %}</b>
• {% trans "Tier:" %} {{ context.tier_name }}
• {% trans "Status:" %} {% trans "Active" %}
{% endif %}
