{% load i18n %}📅 {% blocktranslate with series=context.event_series_name count=context.events_count %}{{ count }} new events scheduled for <b>{{ series }}</b>{% endblocktranslate %}

{% trans "Organization:" %} {{ context.organization_name }}

<a href="{{ context.series_url }}">{% trans "View Series" %}</a>
