{% load i18n %}🎉 {% blocktranslate with org=context.organization_name event=context.event_name %}<b>{{ org }}</b> has published a new event: <b>{{ event }}</b>!{% endblocktranslate %}

<b>{% trans "Event Details:" %}</b>
📅 {{ context.event_start_formatted }}
{% if context.event_end_formatted %}{% trans "Until:" %} {{ context.event_end_formatted }}{% endif %}
{% if context.event_location %}📍 {{ context.event_location }}{% endif %}

{% if context.event_description %}
{{ context.event_description }}
{% endif %}

{% if context.registration_opens_at %}
🎫 <b>{% trans "Registration opens:" %}</b> {{ context.registration_opens_at }}
{% else %}
🎫 <b>{% trans "Registration is now open!" %}</b>
{% endif %}

<a href="{{ context.event_url }}">{% trans "View Event & Register" %}</a>
