{% load i18n %}✅ {% blocktranslate with event=context.event_name %}You have been checked in for <b>{{ event }}</b>{% endblocktranslate %}

<b>{% trans "Event Details:" %}</b>
📅 {{ context.event_start_formatted }}
{% if context.event_location %}📍 {{ context.event_location }}{% endif %}

{% trans "Enjoy the event!" %}
