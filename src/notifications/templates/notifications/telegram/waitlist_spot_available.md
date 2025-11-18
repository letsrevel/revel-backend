{% load i18n %}🎉 <b>{% trans "Great news!" %}</b> {% if context.spots_available == 1 %}{% blocktranslate with count=context.spots_available event=context.event_name %}{{ count }} spot is now available for <b>{{ event }}</b>!{% endblocktranslate %}{% else %}{% blocktranslate with count=context.spots_available event=context.event_name %}{{ count }} spots are now available for <b>{{ event }}</b>!{% endblocktranslate %}{% endif %}

⏰ <b>{% trans "Act fast!" %}</b> {% trans "Spots are limited and available on a first-come, first-served basis." %}

<b>{% trans "Event Details:" %}</b>
📅 {{ context.event_start_formatted }}
{% if context.event_location %}📍 {{ context.event_location }}{% endif %}
🏢 {{ context.organization_name }}
