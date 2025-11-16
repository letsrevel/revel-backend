{% load i18n %}**{{ requester_name }}** {% trans "requested to join" %} **{{ organization_name }}**

{% if request_message %}
> {{ request_message }}
{% endif %}

[{% trans "View Request" %}]({{ frontend_url }})
