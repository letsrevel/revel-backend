{% load i18n %}{% trans "Your custom domain is verified and serves your organization. Visitors who open it see your events with your branding." %}

{% trans "Before you announce it:" %}
- {% trans "Open the link in a private window and check that it loads." %}
- {% trans "Sign up for a test account on it. Sign-up should work on your own domain." %}
- {% trans "Share the new link with your audience." %}

[{% trans "Open your site" %}]({{ context.domain_url }}) · [{% trans "Manage your custom domain" %}]({{ context.action_url }})
