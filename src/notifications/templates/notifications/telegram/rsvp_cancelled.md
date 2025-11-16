{% load i18n %}❌ {% blocktranslate with event=context.event_name %}Your RSVP for <b>{{ event }}</b> has been cancelled.{% endblocktranslate %}

<b>{% trans "Event Details:" %}</b>
📅 {{ context.event_start_formatted }}
{% if context.event_location %}📍 {{ context.event_location }}{% endif %}

{% if context.cancellation_reason %}
<b>{% trans "Reason:" %}</b> {{ context.cancellation_reason }}
{% endif %}

{% trans "You can RSVP again anytime if you change your mind." %}

<a href="{{ context.event_url }}">{% trans "View Event" %}</a>
