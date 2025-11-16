{% load i18n %}{% if context.days_until == 1 %}
⏰ {% blocktranslate with event=context.event_name %}Reminder: <b>{{ event }}</b> is tomorrow!{% endblocktranslate %}
{% else %}
⏰ {% blocktranslate with event=context.event_name days=context.days_until %}Reminder: <b>{{ event }}</b> is in <b>{{ days }} days</b>{% endblocktranslate %}
{% endif %}

<b>{% trans "Event Details:" %}</b>
📅 {{ context.event_start_formatted }}
{% if context.event_end_formatted %}{% trans "Until:" %} {{ context.event_end_formatted }}{% endif %}
{% if context.event_location %}📍 {{ context.event_location }}{% endif %}

{% if context.ticket_id %}
🎫 <b>{% trans "Your Ticket:" %}</b>
• {% trans "Ticket ID:" %} <code>{{ context.ticket_id }}</code>
• {% trans "Tier:" %} {{ context.tier_name }}
{% endif %}

{% if context.reminder_message %}
{{ context.reminder_message }}
{% endif %}

<a href="{{ context.event_url }}">{% trans "View Event" %}</a>

{% trans "See you there!" %} 👋
