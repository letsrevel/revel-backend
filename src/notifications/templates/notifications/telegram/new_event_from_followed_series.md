{% load i18n %}🎉 {% blocktranslate with series=context.event_series_name event=context.event_name %}New event in <b>{{ series }}</b>: <b>{{ event }}</b>{% endblocktranslate %}

<b>{% trans "Event Details:" %}</b>
📅 {{ context.event_start_formatted }}
{% if context.event_location %}📍 {{ context.event_location }}{% endif %}

{% if context.event_description %}
{{ context.event_description|truncatewords:50 }}
{% endif %}

<a href="{{ context.event_url }}">{% trans "View Event" %}</a>
