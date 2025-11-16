{% load i18n %}{% blocktranslate with event=context.event_name %}Your RSVP for **{{ event }}** has been cancelled. ❌{% endblocktranslate %}

**{% trans "Event Details:" %}**
- 📅 {{ context.event_start_formatted }}
{% if context.event_location %}- 📍 {{ context.event_location }}{% endif %}

{% if context.cancellation_reason %}
**{% trans "Reason:" %}** {{ context.cancellation_reason }}
{% endif %}

{% trans "You can RSVP again anytime if you change your mind." %}

[{% trans "View Event" %}]({{ context.event_url }})
