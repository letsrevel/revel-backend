{% load i18n %}**{% trans "New Membership Request" %}**

**{{ context.requester_name }}** {% trans "requested to join" %} **{{ context.organization_name }}**

{% trans "Email:" %} {{ context.requester_email }}
{% if context.tier_name %}{% trans "Tier:" %} {{ context.tier_name }}
{% endif %}{% if context.plan_name %}{% trans "Plan:" %} {{ context.plan_name }}{% if context.plan_payment_method == "offline" %} ({% trans "paid directly to the organization" %}){% endif %}
{% endif %}{% if context.request_message %}
{% trans "Message:" %}
> {{ context.request_message }}
{% endif %}

[{% trans "View Request" %}]({{ context.frontend_url }})
