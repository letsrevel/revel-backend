{% load i18n %}{% if context.ticket_holder_name %}
{% blocktranslate with holder=context.ticket_holder_name event=context.event_name action=context.action %}<b>{{ holder }}</b>'s ticket for <b>{{ event }}</b> has been <b>{{ action }}</b>.{% endblocktranslate %}

<b>{% trans "Ticket Details:" %}</b>
• {% trans "Holder:" %} {{ context.ticket_holder_name }}
• {% trans "Tier:" %} {{ context.tier_name }}
• {% trans "Status:" %} {{ context.ticket_status }}

{% else %}
🔄 {% blocktranslate with event=context.event_name action=context.action %}Your ticket for <b>{{ event }}</b> has been <b>{{ action }}</b>.{% endblocktranslate %}

<b>{% trans "Event Details:" %}</b>
📅 {{ context.event_start_formatted }}
{% if context.event_location %}📍 {{ context.event_location }}{% endif %}

<b>{% trans "Updated Information:" %}</b>
• {% trans "Tier:" %} {{ context.tier_name }}
• {% trans "Status:" %} {{ context.ticket_status }}

{% if context.update_reason %}
<b>{% trans "Reason:" %}</b> {{ context.update_reason }}
{% endif %}
{% endif %}

<a href="{{ context.event_url }}">{% trans "View Event" %}</a>
