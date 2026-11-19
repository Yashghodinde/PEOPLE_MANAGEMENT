from django.urls import path

from . import views

urlpatterns = [
    path("app/chat", views.home),
    path("app/chat/list", views.list_fragment),
    path("app/chat/people", views.people),
    path("app/chat/unread", views.unread),
    path("app/chat/attachable", views.attachable),
    path("app/chat/new", views.new_chat),
    path("app/chat/search", views.search),
    path("app/chat/c/<uuid:conv_id>", views.conversation),
    path("app/chat/c/<uuid:conv_id>/files", views.conversation_files),
    path("app/chat/c/<uuid:conv_id>/messages", views.messages_fragment),
    path("app/chat/c/<uuid:conv_id>/send", views.send),
    path("app/chat/c/<uuid:conv_id>/read", views.read),
    path("app/chat/c/<uuid:conv_id>/manage", views.manage),
    path("app/chat/c/<uuid:conv_id>/typing", views.typing),
    path("app/chat/m/<uuid:message_id>/react", views.react),
    path("app/chat/m/<uuid:message_id>/edit", views.edit),
    path("app/chat/m/<uuid:message_id>/delete", views.delete),
]
