{% load i18n %}{% blocktranslate with follower=context.follower_name series=context.event_series_name %}<b>{{ follower }}</b> started following <b>{{ series }}</b>.{% endblocktranslate %}

<b>{% trans "Follower Details:" %}</b>
• {% trans "Name:" %} {{ context.follower_name }}
• {% trans "Email:" %} {{ context.follower_email }}
• {% trans "Series:" %} {{ context.event_series_name }}
