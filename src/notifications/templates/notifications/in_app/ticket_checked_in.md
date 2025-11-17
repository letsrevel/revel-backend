{% load i18n %}{% blocktranslate with event=context.event_name %}You have been checked in for **{{ event }}**! ✅{% endblocktranslate %}

**{% trans "Event Details:" %}**
- 📅 {{ context.event_start_formatted }}
{% if context.event_location %}- 📍 {{ context.event_location }}{% endif %}

{% trans "Enjoy the event!" %} 🎉

[{% trans "View Event" %}]({{ context.event_url }})
