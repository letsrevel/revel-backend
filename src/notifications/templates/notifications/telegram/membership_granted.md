{%load i18n %}🎊 {%blocktranslate with org=context.organization_name role=context.role %}You are now a <b>{{ role }}</b> of <b>{{ org }}</b>!{%endblocktranslate%}

<a href="{{context.frontend_url}}">{%trans "View Organization"%}</a>
