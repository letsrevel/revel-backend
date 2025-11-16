{% load i18n %}⚠️ {% blocktranslate with event=context.event_name %}Important update about <b>{{ event }}</b>{% endblocktranslate %}

<b>{% trans "Event Details:" %}</b>
📅 {{ context.event_start_formatted }}
{% if context.event_end_formatted %}{% trans "Until:" %} {{ context.event_end_formatted }}{% endif %}
{% if context.event_location %}📍 {{ context.event_location }}{% endif %}

{% if context.changes_summary %}
<b>{% trans "What Changed:" %}</b>
{{ context.changes_summary }}
{% endif %}

{% if context.update_message %}
<b>{% trans "Message from Organizers:" %}</b>
{{ context.update_message }}
{% endif %}

<a href="{{ context.event_url }}">{% trans "View Updated Event" %}</a>
