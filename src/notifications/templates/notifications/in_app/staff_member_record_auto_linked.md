{% load i18n %}{% blocktranslate with member=context.member_display_name record=context.record_full_name org=context.organization_name %}{{ member }} was linked automatically to the register entry {{ record }} in {{ org }} because their verified email matched.{% endblocktranslate %} {% trans "If this is wrong, review the entry in the member register." %}

[{% trans "Open Member Register" %}]({{ context.frontend_url }})
