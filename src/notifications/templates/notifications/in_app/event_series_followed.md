{% load i18n %}{% blocktranslate with follower=context.follower_name series=context.event_series_name %}**{{ follower }}** started following **{{ series }}**.{% endblocktranslate %}

**{% trans "Follower Details:" %}**
- {% trans "Name:" %} {{ context.follower_name }}
- {% trans "Email:" %} {{ context.follower_email }}
- {% trans "Series:" %} {{ context.event_series_name }}
