{% load i18n %}{% if context.user_name %}
{% blocktranslate with user=context.user_name event=context.event_name %}<b>{{ user }}</b> has cancelled their RSVP for <b>{{ event }}</b>.{% endblocktranslate %}

<b>{% trans "Cancellation Details:" %}</b>
• {% trans "User:" %} {{ context.user_name }}
📅 {{ context.event_start_formatted }}
{% if context.event_location %}📍 {{ context.event_location }}{% endif %}

{% if context.cancellation_reason %}<b>{% trans "Reason:" %}</b> {{ context.cancellation_reason }}{% endif %}
{% else %}
❌ {% blocktranslate with event=context.event_name %}Your RSVP for <b>{{ event }}</b> has been cancelled.{% endblocktranslate %}

<b>{% trans "Event Details:" %}</b>
📅 {{ context.event_start_formatted }}
{% if context.event_location %}📍 {{ context.event_location }}{% endif %}

{% if context.cancellation_reason %}<b>{% trans "Reason:" %}</b> {{ context.cancellation_reason }}{% endif %}

{% trans "You can RSVP again anytime if you change your mind." %}
{% endif %}

<a href="{{ context.event_url }}">{% trans "View Event" %}</a>
