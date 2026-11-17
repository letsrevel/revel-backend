{% load i18n %}**{% trans "New Member Awaiting Payment" %}**

{% blocktranslate with name=context.member_name tier=context.tier_name plan=context.plan_name %}**{{ name }}** joined **{{ tier }}** ({{ plan }}) and is awaiting payment.{% endblocktranslate %}

{% trans "Email:" %} {{ context.member_email }}

[{% trans "View Pending Subscriptions" %}]({{ context.frontend_url }})
