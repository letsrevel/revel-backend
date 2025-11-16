{% load i18n %}**{% trans "New Invitation Request" %}**

**{{ context.requester_email }}** {% trans "requested an invitation to" %} **{{ context.event_name }}**

{% if context.requester_name %}{% trans "Name:" %} {{ context.requester_name }}{% endif %}
{% if context.request_message %}
{% trans "Message:" %}
> {{ context.request_message }}
{% endif %}

[{% trans "View Request" %}]({{ context.frontend_url }})
