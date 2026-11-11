from django.apps import AppConfig


class AccountsConfig(AppConfig):
    name = "ots.accounts"
    label = "accounts"

    def ready(self) -> None:
        # Our column is last_login_at, set by the login service; Django's stock signal would
        # try to save a `last_login` column that does not exist.
        from django.contrib.auth import user_logged_in
        from django.contrib.auth.models import update_last_login

        user_logged_in.disconnect(update_last_login, dispatch_uid="update_user_last_login")
        from . import permissions  # noqa: F401  (registers policies)
