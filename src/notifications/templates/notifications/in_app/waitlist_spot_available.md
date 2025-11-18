{% load i18n %}**{% trans "Great news!" %}** {% if context.spots_available == 1 %}{% blocktranslate with count=context.spots_available event=context.event_name %}{{ count }} spot is now available for **{{ event }}**!{% endblocktranslate %}{% else %}{% blocktranslate with count=context.spots_available event=context.event_name %}{{ count }} spots are now available for **{{ event }}**!{% endblocktranslate %}{% endif %}

⏰ **{% trans "Act fast!" %}** {% trans "Spots are limited and available on a first-come, first-served basis." %}

**{% trans "Event Details:" %}**
- 📅 {{ context.event_start_formatted }}
{% if context.event_location %}- 📍 {{ context.event_location }}{% endif %}
- 🏢 {{ context.organization_name }}

[{% trans "Claim Your Spot Now" %}]({{ context.event_url }})
