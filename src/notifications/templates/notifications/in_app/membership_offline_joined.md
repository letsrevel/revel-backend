{% load i18n %}{% blocktranslate with name=context.member_name tier=context.tier_name plan=context.plan_name %}**{{ name }}** joined **{{ tier }}** ({{ plan }}) and is awaiting payment.{% endblocktranslate %}

[{% trans "View Pending Subscriptions" %}]({{ context.frontend_url }})
