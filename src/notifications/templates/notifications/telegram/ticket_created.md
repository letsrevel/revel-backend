{% load i18n %}{% if context.ticket_holder_name %}
{% blocktranslate with holder=context.ticket_holder_name event=context.event_name %}<b>{{ holder }}</b> has registered for <b>{{ event }}</b>.{% endblocktranslate %}

<b>{% trans "Ticket Details:" %}</b>
• {% trans "Holder:" %} {{ context.ticket_holder_name }}
• {% trans "Tier:" %} {{ context.tier_name }}
• {% trans "Status:" %} {{ context.ticket_status }}
• {% trans "Ticket ID:" %} <code>{{ context.ticket_id }}</code>

{% else %}{% if context.ticket_status == "pending" %}⏳ {% blocktranslate with event=context.event_name %}Ticket Pending for <b>{{ event }}</b>{% endblocktranslate %}

<b>{% trans "Event Details:" %}</b>
📅 {{ context.event_start_formatted }}
{% if context.event_location %}📍 {{ context.event_location }}{% endif %}

<b>{% trans "Ticket Information:" %}</b>
• {% trans "Tier:" %} {{ context.tier_name }}
• {% trans "Status:" %} {% trans "Pending" %}

{% if context.manual_payment_instructions %}<b>{% trans "Payment Instructions:" %}</b>
<blockquote>{{ context.manual_payment_instructions }}</blockquote>
{% else %}<i>{% trans "Please contact the organizer to complete the payment." %}</i>
{% endif %}{% else %}✅ {% blocktranslate with event=context.event_name %}Ticket Confirmed for <b>{{ event }}</b>{% endblocktranslate %}

<b>{% trans "Event Details:" %}</b>
📅 {{ context.event_start_formatted }}
{% if context.event_location %}📍 {{ context.event_location }}{% endif %}

<b>{% trans "Ticket Information:" %}</b>
• {% trans "Tier:" %} {{ context.tier_name }}
• {% trans "Status:" %} {% trans "Active" %}
{% endif %}{% endif %}
