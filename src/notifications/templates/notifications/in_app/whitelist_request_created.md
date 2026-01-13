{% load i18n %}**{{ context.requester_name }}** {% trans "requested verification for" %} **{{ context.organization_name }}**

{% trans "Matched Entries:" %} {{ context.matched_entries_count }}

{% if context.request_message %}
> {{ context.request_message }}
{% endif %}

[{% trans "Review Request" %}]({{ context.frontend_url }})
