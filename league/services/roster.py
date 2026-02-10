# league/services/roster.py
from django.utils import timezone
from django.db import transaction
from django.core.exceptions import ValidationError

from league.models import Draft, RosterSpot, Transaction


def _get_current_draft():
    draft = Draft.objects.order_by("-id").first()
    if not draft:
        raise ValidationError("Nenhum draft encontrado.")
    return draft


def is_player_free_agent(player) -> bool:
    """
    True se o player NÃO está em nenhum roster ativo (dropped_at is null).
    """
    draft = _get_current_draft()
    return not RosterSpot.objects.filter(draft=draft, player=player, dropped_at__isnull=True).exists()


def team_owns_player(team, player) -> bool:
    """
    True se o player está no roster ativo do team.
    """
    draft = _get_current_draft()
    return RosterSpot.objects.filter(draft=draft, team=team, player=player, dropped_at__isnull=True).exists()


@transaction.atomic
def drop_player_from_roster(team, player):
    """
    Drop lógico (marca dropped_at).
    """
    draft = _get_current_draft()
    spot = RosterSpot.objects.filter(
        draft=draft,
        team=team,
        player=player,
        dropped_at__isnull=True,
    ).first()

    if not spot:
        raise ValidationError("Drop inválido: jogador não está no roster ativo do time.")

    spot.dropped_at = timezone.now()
    spot.save(update_fields=["dropped_at"])

    Transaction.objects.create(
        draft=draft,
        team=team,
        player=player,
        type="DROP",
    )


@transaction.atomic
def add_player_to_roster(team, player):
    """
    Add/reativa roster spot.
    """
    draft = _get_current_draft()

    # segurança: não permitir add se já está ativo em alguém
    if RosterSpot.objects.filter(draft=draft, player=player, dropped_at__isnull=True).exists():
        raise ValidationError("Add inválido: jogador já pertence a um time.")

    spot = RosterSpot.objects.filter(draft=draft, player=player).first()
    if spot:
        spot.team = team
        spot.acquired_via = "WAIVER"
        spot.acquired_at = timezone.now()
        spot.dropped_at = None
        spot.save()
    else:
        RosterSpot.objects.create(
            draft=draft,
            team=team,
            player=player,
            acquired_via="WAIVER",
            acquired_at=timezone.now(),
        )

    Transaction.objects.create(
        draft=draft,
        team=team,
        player=player,
        type="ADD",
        notes="WAIVER",
    )
