from django import template


register = template.Library()


@register.filter
def comma(value):
    try:
        return f'{int(value):,}'
    except (TypeError, ValueError):
        try:
            return f'{float(value):,}'
        except (TypeError, ValueError):
            return value


@register.filter
def get_item(mapping, key):
    if not mapping:
        return 0
    return mapping.get(key, 0)
