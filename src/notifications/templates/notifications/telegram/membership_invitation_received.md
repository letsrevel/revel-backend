{%load i18n %}{%blocktranslate with org=context.organization_name tier=context.tier_name %}<b>{{ org }}</b> invited you to become a member ({{ tier }}). You only become a member if you accept.{%endblocktranslate%}

<a href="{{context.accept_url}}">{%trans "Accept invitation" %}</a>

{%blocktranslate with expires=context.expires_at %}This invitation expires on {{ expires }}. If you don't want to join, you can ignore it.{%endblocktranslate%}
