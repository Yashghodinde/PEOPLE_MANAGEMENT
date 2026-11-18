"""Template tags for the component library.

Leaf components are plain include partials; tags exist only for components that wrap content
(card, modal, drawer) and for icons and status badges.
"""

from typing import Any

from django import template
from django.template.loader import render_to_string
from django.templatetags.static import static
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from ots.ui import status

register = template.Library()


@register.simple_tag
def icon(name: str, size: str = "4", extra: str = "") -> str:
    sizes = {"3": "w-3 h-3", "4": "w-4 h-4", "5": "w-5 h-5"}
    return format_html(
        '<svg class="{} {} shrink-0" fill="none" stroke="currentColor" stroke-width="2" '
        'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><use href="{}#i-{}"/></svg>',
        sizes.get(size, "w-4 h-4"),
        extra,
        static("vendor/lucide-sprite.svg"),
        name,
    )


@register.simple_tag
def badge(value: Any, label: str = "") -> str:
    return render_to_string(
        "components/badge.html", {"tone": status.tone(value), "label": label or status.label(value)}
    )


@register.filter
def get_item(mapping: Any, key: str) -> Any:
    """{{ values|get_item:name }}: dict lookup with a name held in a variable; missing gives ''."""
    return mapping.get(key, "") if hasattr(mapping, "get") else ""


@register.filter
def nth(items: Any, index: int) -> Any:
    """items[index], or an empty dict when there is no such entry (a table cell's column, for its label)."""
    try:
        return items[index]
    except (IndexError, TypeError, KeyError):
        return {}


@register.filter
def tone(value: Any) -> str:
    return status.tone(value)


class _Block(template.Node):
    """Render a wrapping component: the block body becomes `body` in the component template."""

    def __init__(self, template_name: str, nodelist: template.NodeList, kwargs: dict[str, Any]) -> None:
        self.template_name, self.nodelist, self.kwargs = template_name, nodelist, kwargs

    def render(self, context: template.Context) -> str:
        values = {k: v.resolve(context) for k, v in self.kwargs.items()}
        values["body"] = mark_safe(self.nodelist.render(context))  # noqa: S308  (already-rendered template)
        return render_to_string(self.template_name, values, request=context.get("request"))


def _block_tag(tag_name: str, template_name: str) -> None:
    def compile_fn(parser: template.base.Parser, token: template.base.Token) -> _Block:
        bits = token.split_contents()[1:]
        kwargs = {}
        for bit in bits:
            key, _, expr = bit.partition("=")
            kwargs[key] = parser.compile_filter(expr)
        nodelist = parser.parse((f"end{tag_name}",))
        parser.delete_first_token()
        return _Block(template_name, nodelist, kwargs)

    register.tag(tag_name, compile_fn)


_block_tag("card", "components/card.html")
_block_tag("modal", "components/modal.html")
_block_tag("drawer", "components/drawer.html")


@register.simple_tag
def dot(dots: Any, task_id: Any) -> Any:
    """The small coloured circle beside a task with something new: red message, green finished, grey other."""
    kind = (dots or {}).get(str(task_id))
    if not kind:
        return ""
    label = {"chat": "New message", "done": "Finished", "update": "Updated"}[kind]
    return format_html(
        '<span class="ots-dot ots-dot-{}" title="{}" role="img" aria-label="{}"></span>', kind, label, label
    )


@register.simple_tag(takes_context=True)
def pagehead(context: Any) -> str:
    """The shared page header (IMP-14.1): breadcrumb and a visible Back, from the path and page_title."""
    from ots.ui.crumbs import trail

    request = context.get("request")
    if request is None:
        return ""
    crumbs = trail(request.path, str(context.get("page_title") or ""))
    if not crumbs:
        return ""
    back = str(context.get("back_url") or "") or next((u for _l, u in reversed(crumbs[:-1]) if u), "")
    return render_to_string("components/pagehead.html", {"crumbs": crumbs, "back": back})
