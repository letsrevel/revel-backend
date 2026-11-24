{%load i18n %}{%blocktranslate with org=context.organization_name %}{{ org }} keeps a member register. It has an entry with your email address. You verified this address, so we linked the entry to your account.{%endblocktranslate%}

{%trans "If this entry is not yours, or you do not want to be a member, contact the organization." %}

<a href="{{context.frontend_url}}">{%trans "Open the organization page" %}</a>
