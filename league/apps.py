from django.apps import AppConfig


class LeagueConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "league"

    def ready(self):
        # Não carregamos mais signals aqui.
        # A criação do roster já é tratada em DraftPick.save().
        pass
