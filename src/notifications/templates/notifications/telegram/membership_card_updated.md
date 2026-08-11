{%load i18n %}🎫 {%blocktranslate with org=context.organization_name tier=context.tier_name %}Your membership tier in <b>{{ org }}</b> changed to <b>{{ tier }}</b>. Re-add your wallet card to see the update.{%endblocktranslate%}

<a href="{{context.frontend_url}}">{%trans "View Organization"%}</a>
