{% load i18n %}❌ {% blocktranslate with event=context.event_name %}<b>{{ event }}</b> has been cancelled.{% endblocktranslate %}

<b>{% trans "Event Details:" %}</b>
📅 {{ context.event_start_formatted }}
{% if context.event_location %}📍 {{ context.event_location }}{% endif %}

{% if context.cancellation_reason %}
<b>{% trans "Reason:" %}</b>
{{ context.cancellation_reason|escape }}
{% endif %}

{% if context.refund_available %}
💰 <b>{% trans "Refund Information:" %}</b>
{% trans "Refunds will be processed according to the organizer's refund policy." %}
{% endif %}

{% if context.alternative_event_url %}
<a href="{{ context.alternative_event_url }}">{% trans "View Alternative Event" %}</a>
{% endif %}

{% trans "We apologize for any inconvenience." %}
