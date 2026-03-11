{% load i18n %}💌 {% blocktranslate with event=context.event_name %}<b>You're invited to {{ event }}</b>{% endblocktranslate %}

{% if context.invitation_message %}
{{ context.invitation_message }}
{% endif %}

<b>{% trans "Event Details:" %}</b>
📅 {{ context.event_start_formatted }}
{% if context.event_end_formatted %}{% trans "Until:" %} {{ context.event_end_formatted }}{% endif %}
{% if context.event_location %}📍 {{ context.event_location }}{% endif %}

{% if context.rsvp_required %}
🎉 <b>{% trans "RSVP Required" %}</b>
{% else %}
🎫 <b>{% trans "Tickets Required" %}</b>
{% endif %}
