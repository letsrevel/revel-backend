{% load i18n %}{% blocktranslate with follower=context.follower_name org=context.organization_name %}**{{ follower }}** started following **{{ org }}**.{% endblocktranslate %}

**{% trans "Follower Details:" %}**
- {% trans "Name:" %} {{ context.follower_name }}
- {% trans "Email:" %} {{ context.follower_email }}
