{% load i18n %}⚠️ {% blocktranslate with event=context.event_name %}Important update about **{{ event }}**{% endblocktranslate %}

**{% trans "Event Details:" %}**
- 📅 {{ context.event_start_formatted }}
{% if context.event_end_formatted %}- {% trans "Until:" %} {{ context.event_end_formatted }}{% endif %}
{% if context.event_location %}- 📍 {{ context.event_location }}{% endif %}

{% if context.changes_summary %}
**{% trans "What Changed:" %}**
{{ context.changes_summary }}
{% endif %}

{% if context.update_message %}
**{% trans "Message from Organizers:" %}**
{{ context.update_message }}
{% endif %}

[{% trans "View Updated Event" %}]({{ context.event_url }})
