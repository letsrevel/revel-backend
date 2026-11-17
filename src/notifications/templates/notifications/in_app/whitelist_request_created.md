{% load i18n %}{% blocktranslate with name=context.requester_name org=context.organization_name %}**{{ name }}** requested verification for **{{ org }}**.{% endblocktranslate %}

{% trans "Matched Entries:" %} {{ context.matched_entries_count }}

{% if context.request_message %}
> {{ context.request_message }}
{% endif %}

[{% trans "Review Request" %}]({{ context.frontend_url }})
