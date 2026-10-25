{% load i18n %}{% blocktranslate with event=context.event_name %}❌ **{{ event }}** has been cancelled.{% endblocktranslate %}

**{% trans "Event Details:" %}**
- 📅 {{ context.event_start_formatted }}
{% if context.event_location %}- 📍 {{ context.event_location }}{% endif %}

{% if context.cancellation_reason %}
**{% trans "Reason:" %}**
{{ context.cancellation_reason }}
{% endif %}

{% if context.refund_available %}
💰 **{% trans "Refund Information:" %}**
{% trans "Refunds will be processed according to the organizer's refund policy." %}
{% endif %}

{% if context.alternative_event_url %}
[{% trans "View Alternative Event" %}]({{ context.alternative_event_url }})
{% endif %}

{% trans "We apologize for any inconvenience this may cause." %}
