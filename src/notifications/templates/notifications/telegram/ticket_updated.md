{% load i18n %}{% if context.ticket_holder_name %}
{% blocktranslate with holder=context.ticket_holder_name event=context.event_name action=context.action %}<b>{{ holder }}</b>'s ticket for <b>{{ event }}</b> has been <b>{{ action }}</b>.{% endblocktranslate %}

<b>{% trans "Ticket Details:" %}</b>
• {% trans "Holder:" %} {{ context.ticket_holder_name }}
• {% trans "Tier:" %} {{ context.tier_name }}
• {% trans "Status:" %} {{ context.ticket_status }}

{% else %}{% if context.old_status == "pending" and context.new_status == "active" %}✅ {% blocktranslate with event=context.event_name %}Ticket Confirmed for <b>{{ event }}</b>{% endblocktranslate %}

<b>{% trans "Event Details:" %}</b>
📅 {{ context.event_start_formatted }}
{% if context.event_location %}📍 {{ context.event_location }}{% endif %}

<b>{% trans "Ticket Information:" %}</b>
• {% trans "Tier:" %} {{ context.tier_name }}
• {% trans "Status:" %} {% trans "Active" %}

{% trans "Your payment has been confirmed!" %}
{% else %}🔄 {% blocktranslate with event=context.event_name %}Ticket Update for <b>{{ event }}</b>{% endblocktranslate %}

<b>{% trans "Event Details:" %}</b>
📅 {{ context.event_start_formatted }}
{% if context.event_location %}📍 {{ context.event_location }}{% endif %}

<b>{% trans "Updated Information:" %}</b>
• {% trans "Tier:" %} {{ context.tier_name }}
• {% trans "Status:" %} {{ context.ticket_status }}

{% if context.reason %}
<b>{% trans "Reason:" %}</b> {{ context.reason }}
{% endif %}{% endif %}{% endif %}
