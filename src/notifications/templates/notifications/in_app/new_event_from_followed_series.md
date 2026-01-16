{% load i18n %}🎉 {% blocktranslate with series=context.event_series_name event=context.event_name %}New event in **{{ series }}**: **{{ event }}**{% endblocktranslate %}

**{% trans "Event Details:" %}**
- 📅 {{ context.event_start_formatted }}
{% if context.event_location %}- 📍 {{ context.event_location }}{% endif %}
- {% trans "Series:" %} {{ context.event_series_name }}

{% if context.event_description %}
{{ context.event_description|truncatewords:50 }}
{% endif %}

[{% trans "View Event" %}]({{ context.event_url }})
