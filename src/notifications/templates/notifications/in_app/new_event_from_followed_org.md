{% load i18n %}🎉 {% blocktranslate with org=context.organization_name event=context.event_name %}**{{ org }}** created a new event: **{{ event }}**{% endblocktranslate %}

**{% trans "Event Details:" %}**
- 📅 {{ context.event_start_formatted }}
{% if context.event_location %}- 📍 {{ context.event_location }}{% endif %}

{% if context.event_description %}
{{ context.event_description|truncatewords:50 }}
{% endif %}

[{% trans "View Event" %}]({{ context.event_url }})
