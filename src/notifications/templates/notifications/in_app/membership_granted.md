{%load i18n %}🎊 {%blocktranslate with org=context.organization_name role=context.role %}You are now a **{{ role }}** of **{{ org }}**!{%endblocktranslate%}

[{%trans "View Organization"%}]({{context.frontend_url}})
