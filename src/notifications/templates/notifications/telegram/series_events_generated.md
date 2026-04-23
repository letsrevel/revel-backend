{% load i18n %}📅 {% blocktranslate with series=context.event_series_name count counter=context.events_count %}{{ counter }} new event scheduled for <b>{{ series }}</b>{% plural %}{{ counter }} new events scheduled for <b>{{ series }}</b>{% endblocktranslate %}

{% trans "Organization:" %} {{ context.organization_name }}

<a href="{{ context.series_url }}">{% trans "View Series" %}</a>
