{% load i18n %}❌ {% blocktranslate with event=context.event_name %}<b>{{ event }}</b> has been cancelled.{% endblocktranslate %}

<b>{% trans "Event Details:" %}</b>
📅 {{ context.event_start_formatted }}
{% if context.event_location %}📍 {{ context.event_location }}{% endif %}

{% if context.cancellation_reason %}
<b>{% trans "Reason:" %}</b>
{{ context.cancellation_reason }}
{% endif %}

{% if context.refund_info %}
💰 <b>{% trans "Refund Information:" %}</b>
{{ context.refund_info }}
{% endif %}

{% if context.alternative_event_url %}
<a href="{{ context.alternative_event_url }}">{% trans "View Alternative Event" %}</a>
{% endif %}

{% trans "We apologize for any inconvenience." %}
