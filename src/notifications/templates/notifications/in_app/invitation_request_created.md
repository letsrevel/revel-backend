{% load i18n %}**{{ context.requester_email }}** {% trans "requested an invitation to" %} **{{ context.event_name }}**

{% if context.request_message %}
> {{ context.request_message }}
{% endif %}

[{% trans "View Request" %}]({{ context.frontend_url }})
