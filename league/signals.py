from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import DraftPick, Roster


@receiver(post_save, sender=DraftPick)
def add_player_to_roster_when_drafted(sender, instance: DraftPick, created, **kwargs):
    # Só faz algo quando o pick tem player
    if not instance.player:
        return

    # Garante que o jogador entre no roster do time
    Roster.objects.get_or_create(
        team=instance.team,
        player=instance.player,
        defaults={"active": True},
    )
