{% load i18n %}📅 {% blocktranslate with series=context.event_series_name count=context.events_count %}{{ count }} new events scheduled for **{{ series }}**{% endblocktranslate %}

{% trans "Organization:" %} {{ context.organization_name }}

[{% trans "View Series" %}]({{ context.series_url }})
