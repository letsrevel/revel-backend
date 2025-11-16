{% load i18n %}{% if context.user_name %}
{% blocktranslate with user=context.user_name event=context.event_name response=context.response %}**{{ user }}** has RSVP'd **{{ response }}** for **{{ event }}**.{% endblocktranslate %}

**{% trans "RSVP Details:" %}**
- {% trans "User:" %} {{ context.user_name }} ({{ context.user_email }})
- {% trans "Response:" %} {{ context.response }}
- {% trans "RSVP ID:" %} `{{ context.rsvp_id }}`

{% else %}
{% blocktranslate with event=context.event_name response=context.response %}Your RSVP (**{{ response }}**) for **{{ event }}** has been confirmed! ✅{% endblocktranslate %}

**{% trans "Event Details:" %}**
- 📅 {{ context.event_start_formatted }}
{% if context.event_location %}- 📍 {{ context.event_location }}{% endif %}

**{% trans "Your Response:" %}**
- {{ context.response }}
{% if context.guest_count %}- {% trans "Guests:" %} {{ context.guest_count }}{% endif %}
{% if context.dietary_restrictions %}- {% trans "Dietary restrictions:" %} {{ context.dietary_restrictions }}{% endif %}
{% endif %}

[{% trans "View Event" %}]({{ context.event_url }})
