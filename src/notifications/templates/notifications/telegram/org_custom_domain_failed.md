{% load i18n %}{% if context.was_live %}{% trans "Revel no longer serves your organization on this domain, and everyone who was signed in on it was signed out. Your organization is still available at its Revel address." %}{% else %}{% trans "Revel could not verify this domain, so it does not serve your organization. Your organization is still available at its Revel address." %}{% endif %}

{% trans "What Revel found:" %}
{% for error in context.errors %}- {{ error }}
{% empty %}- {% trans "No details were reported. If this keeps happening, contact Revel support." %}
{% endfor %}
{% trans "Fix the cause, then request the domain again from your custom domain settings." %}

[{% trans "Open custom domain settings" %}]({{ context.action_url }})
