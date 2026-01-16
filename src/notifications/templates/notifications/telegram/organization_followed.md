{% load i18n %}{% blocktranslate with follower=context.follower_name org=context.organization_name %}<b>{{ follower }}</b> started following <b>{{ org }}</b>.{% endblocktranslate %}

<b>{% trans "Follower Details:" %}</b>
• {% trans "Name:" %} {{ context.follower_name }}
• {% trans "Email:" %} {{ context.follower_email }}
